#!/usr/bin/env python3
import argparse
import json
import logging
import os
import subprocess
from pathlib import Path
from typing import Any, Iterator, Literal, NamedTuple, Optional, Self

import pydantic

logging.basicConfig(level="DEBUG", format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)


class CamelModel(pydantic.BaseModel):
    """Attributes automatically get CamelCase aliases.

    >>> class GolangModel(CamelModel):
            some_attribute: str

    >>> GolangModel.parse_obj({"SomeAttribute": "hello"})
    GolangModel(some_attribute="hello")
    """

    class Config:
        @staticmethod
        def alias_generator(attr_name: str) -> str:
            return "".join(word.capitalize() for word in attr_name.split("_"))

        allow_population_by_field_name = True


class Module(CamelModel):
    """A Go module as returned by the -json option of various commands."""

    path: str
    version: Optional[str] = None
    replace: Optional["Module"] = None
    main: bool = False


class Package(CamelModel):
    """A Go package as returned by the -json option of various commands."""

    import_path: str
    module: Optional[Module]
    standard: bool = False


class NameVersion(NamedTuple):
    """A name and version of a package/module."""

    name: str
    version: str

    def __str__(self) -> str:
        return f"{self.name}@{self.version}"

    @classmethod
    def from_module(cls, module: Module) -> Self:
        """Get the name and version of a Go module."""
        if not (replace := module.replace):
            name = module.path
            version = module.version
        elif replace.version:
            name = replace.path
            version = replace.version
        else:
            name = module.path
            version = replace.path

        if not version:
            raise ValueError(f"versionless module: {module}")

        return cls(name, version)


class GomodResolver:
    """Resolves the dependencies (modules) of a Go module."""

    def __init__(self, module_dir: Path, gomodcache: Path, go_executable: str = "go") -> None:
        """Create a GomodResolver.

        :param module_dir: path to the root of a Go module
        :param gomodcache: path to a GOMODCACHE directory (will be created if it doesn't exist)
        :param go_executable: specify a different `go` executable
        """
        self.module_dir = module_dir
        self.gomodcache = gomodcache.resolve()
        self.go_executable = go_executable

    def parse_download(self) -> set[NameVersion]:
        """Parse modules from `go mod download -json`."""
        log.debug("parsing `go mod download -json`")
        return {
            NameVersion.from_module(module)
            for module in map(
                Module.parse_obj,
                _load_json_stream(self._run_go(["mod", "download", "-json"])),
            )
            if not module.main
        }

    def parse_list_deps(self, pattern: Literal["all", "./..."] = "all") -> set[NameVersion]:
        """Parse modules from `go list -deps -json ./...` or `go list -deps -json all`."""
        log.debug("parsing `go list -deps -json %s`", pattern)
        return {
            NameVersion.from_module(package.module)
            for package in map(
                Package.parse_obj,
                _load_json_stream(self._run_go(["list", "-deps", "-json", pattern])),
            )
            if package.module and not package.module.main
        }

    def parse_gomodcache(self) -> set[NameVersion]:
        """Parse modules from the module cache.

        https://go.dev/ref/mod#module-cache
        """
        log.debug("parsing $GOMODCACHE/cache/download/**/*.zip")
        download_dir = self.gomodcache / "cache" / "download"

        def un_exclamation_mark(s: str) -> str:
            first, *rest = s.split("!")
            return first + "".join(map(str.capitalize, rest))

        def parse_zipfile_path(zipfile: Path) -> NameVersion:
            # filepath ends with @v/<version>.zip
            name = zipfile.relative_to(download_dir).parent.parent.as_posix()
            version = zipfile.stem
            return NameVersion(un_exclamation_mark(name), un_exclamation_mark(version))

        return set(map(parse_zipfile_path, download_dir.rglob("*.zip")))

    def parse_vendor(self, drop_unused: bool = True) -> set[NameVersion]:
        """Parse modules from vendor/modules.txt.

        :param drop_unused: don't include modules that have no packages in modules.txt
        """
        if drop_unused:
            log.debug("parsing vendor/modules.txt")
        else:
            log.debug("parsing vendor/modules.txt (and keeping unused modules)")

        modules_txt = self.module_dir / "vendor" / "modules.txt"

        def parse_module_line(line: str) -> Module:
            match line.removeprefix("# ").split():
                case [name]:
                    return Module(path=name, main=True)
                case [name, version]:
                    return Module(path=name, version=version)
                case [name, "=>", path]:
                    return Module(path=name, replace=Module(path=path))
                case [name, version, "=>", path]:
                    return Module(path=name, version=version, replace=Module(path=path))
                case [name, "=>", new_name, new_version]:
                    return Module(path=name, replace=Module(path=new_name, version=new_version))
                case [name, version, "=>", new_name, new_version]:
                    return Module(
                        path=name,
                        version=version,
                        replace=Module(path=new_name, version=new_version),
                    )
                case _:
                    raise ValueError(f"unrecognized module line: {line}")

        modules: list[Module] = []
        module_has_packages: list[bool] = []

        for line in modules_txt.read_text().splitlines():
            if line.startswith("# "):  # module line
                modules.append(parse_module_line(line))
                module_has_packages.append(False)
            elif not line.startswith("#"):  # package line
                if not modules:
                    raise ValueError(f"no module line found above {line!r}")
                module_has_packages[-1] = True
            elif not line.startswith("## explicit"):  # marker line
                raise ValueError(f"unrecognized line in modules.txt: {line}")

        def is_wildcard_replacement(module: Module) -> bool:
            return module.replace is not None and not module.version

        return {
            NameVersion.from_module(module)
            for module, has_packages in zip(modules, module_has_packages)
            if (
                not module.main
                and not is_wildcard_replacement(module)
                and (has_packages or not drop_unused)
            )
        }

    def _run_go(self, go_cmd: list[str]) -> str:
        cmd = [self.go_executable, *go_cmd]
        p = subprocess.run(
            cmd,
            cwd=self.module_dir,
            env=os.environ | {"GOMODCACHE": str(self.gomodcache)},
            check=True,
            text=True,
            stdout=subprocess.PIPE,
        )
        return p.stdout


def _load_json_stream(json_stream: str) -> Iterator[dict[str, Any]]:
    decoder = json.JSONDecoder()
    i = 0

    while i < len(json_stream):
        data, j = decoder.raw_decode(json_stream, i)
        i = j + 1
        yield data


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("-d", "--module-dir", default=".")
    ap.add_argument("--gomodcache", default="./gocache")
    ap.add_argument("--go", default="go")
    ap.add_argument("-o", "--output-dir", default=".")

    args = ap.parse_args()

    module_dir = Path(args.module_dir)
    gomodcache = Path(args.gomodcache)
    go_executable = str(Path(args.go).expanduser())
    output_dir = Path(args.output_dir)

    resolver = GomodResolver(module_dir, gomodcache, go_executable)

    def write_results(results: list[NameVersion], filename: str) -> None:
        log.debug("writing %s", output_dir / filename)
        output_dir.mkdir(parents=True, exist_ok=True)
        output_dir.joinpath(filename).write_text("\n".join(map(str, results)))

    write_results(sorted(resolver.parse_download()), "download.txt")
    write_results(sorted(resolver.parse_gomodcache()), "gomodcache.txt")
    write_results(sorted(resolver.parse_list_deps(pattern="all")), "listdeps_all.txt")
    write_results(sorted(resolver.parse_list_deps(pattern="./...")), "listdeps_threedot.txt")
    if module_dir.joinpath("vendor").exists():
        write_results(sorted(resolver.parse_vendor()), "vendor.txt")
        write_results(sorted(resolver.parse_vendor(drop_unused=False)), "vendor_with_unused.txt")


if __name__ == "__main__":
    main()
