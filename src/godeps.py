#!/usr/bin/env python3
import argparse
import contextlib
import difflib
import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, ContextManager, Iterable, Iterator, Literal, NamedTuple, Optional, Self

import pydantic

logging.basicConfig(level="DEBUG", format="godeps: %(message)s")
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
    deps: list[str] = []


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


def get_names_and_versions(modules: Iterable[Module]) -> list[NameVersion]:
    return sorted({NameVersion.from_module(module) for module in modules if not module.main})


def get_module_names_and_versions(packages: Iterable[Package]) -> list[NameVersion]:
    return get_names_and_versions(package.module for package in packages if package.module)


class GomodResolver:
    """Resolves the dependencies of a Go module."""

    def __init__(self, module_dir: Path, gomodcache: Path, go_executable: str = "go") -> None:
        """Create a GomodResolver.

        :param module_dir: path to the root of a Go module
        :param gomodcache: absolute path to GOMODCACHE directory (gets created if it doesn't exist)
        :param go_executable: specify a different `go` executable
        """
        self.module_dir = module_dir
        self.gomodcache = gomodcache
        self.go_executable = go_executable

    def parse_download(self) -> list[Module]:
        """Parse modules from `go mod download -json`."""
        return list(
            map(Module.parse_obj, _load_json_stream(self._run_go(["mod", "download", "-json"])))
        )

    def parse_list_deps(self, pattern: Literal["all", "./..."] = "all") -> list[Package]:
        """Parse packages from `go list -deps -json ./...` or `go list -deps -json all`."""
        return list(
            map(
                Package.parse_obj,
                _load_json_stream(
                    self._run_go(
                        ["list", "-deps", "-json=ImportPath,Module,Standard,Deps", pattern]
                    )
                ),
            )
        )

    def parse_gomodcache(self) -> list[Module]:
        """Parse modules from the module cache.

        https://go.dev/ref/mod#module-cache
        """
        download_dir = self.gomodcache / "cache" / "download"

        def un_exclamation_mark(s: str) -> str:
            first, *rest = s.split("!")
            return first + "".join(map(str.capitalize, rest))

        def parse_zipfile_path(zipfile: Path) -> Module:
            # filepath ends with @v/<version>.zip
            name = zipfile.relative_to(download_dir).parent.parent.as_posix()
            version = zipfile.stem
            return Module(path=un_exclamation_mark(name), version=un_exclamation_mark(version))

        return list(map(parse_zipfile_path, download_dir.rglob("*.zip")))

    def vendor_deps(self) -> None:
        """Run `go mod vendor` to vendor dependencies."""
        self._run_go(["mod", "vendor"])

    def parse_vendor(self, drop_unused: bool = True) -> list[Module]:
        """Parse modules from vendor/modules.txt.

        :param drop_unused: don't include modules that have no packages in modules.txt
        """
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

        return [
            module
            for module, has_packages in zip(modules, module_has_packages, strict=True)
            if (
                not module.main
                and not is_wildcard_replacement(module)
                and (has_packages or not drop_unused)
            )
        ]

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
    """Run the CLI."""
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "-m", "--module-dir", default=".", help="path to Go module, defaults to current dir"
    )
    ap.add_argument(
        "-c", "--gomodcache-dir", help="path to GOMODCACHE directory, defaults to a tmpdir"
    )
    ap.add_argument(
        "-o",
        "--output-dir",
        default=".",
        help="write output files to this directory, defaults to current dir",
    )
    ap.add_argument(
        "--vendor", action="store_true", help="vendor dependencies instead of downloading"
    )
    ap.add_argument(
        "--deptree", action="store_true", help="print the dependency tree of the *packages* used"
    )
    ap.add_argument("--go", default="go", help="the go executable to use, defaults to 'go'")

    args = ap.parse_args()

    module_dir = Path(args.module_dir)
    output_dir = Path(args.output_dir)
    go_executable = str(Path(args.go).expanduser())

    if args.gomodcache_dir:
        gomodcache_dir = Path(args.gomodcache_dir).resolve()
        cleanup_context: ContextManager[Any] = contextlib.nullcontext()
    else:
        tmpdir = tempfile.TemporaryDirectory(prefix="godeps-gomodcache-")
        gomodcache_dir = Path(tmpdir.name)
        cleanup_context = tmpdir

    with cleanup_context:
        resolver = GomodResolver(module_dir, gomodcache_dir, go_executable)
        if args.vendor:
            _check_vendor(resolver, output_dir)
        else:
            _check_download(resolver, output_dir)

        if args.deptree:
            _print_deptree(resolver)


def _check_download(resolver: GomodResolver, output_dir: Path) -> None:
    log.info("downloading and identifying dependencies")
    download = get_names_and_versions(resolver.parse_download())
    gomodcache = get_names_and_versions(resolver.parse_gomodcache())
    listdeps_all = get_module_names_and_versions(resolver.parse_list_deps(pattern="all"))
    listdeps_threedot = get_module_names_and_versions(resolver.parse_list_deps(pattern="./..."))

    _write_results(download, output_dir / "download.txt")
    _write_results(gomodcache, output_dir / "gomodcache.txt")
    _write_results(listdeps_all, output_dir / "listdeps_all.txt")
    _write_results(listdeps_threedot, output_dir / "listdeps_threedot.txt")

    if download_diff := _get_diff(map(str, download), map(str, gomodcache)):
        log.info("diffing downloaded modules: identified x actual")
        print(download_diff)
    else:
        log.info("diffing downloaded modules: perfect match")


def _check_vendor(resolver: GomodResolver, output_dir: Path) -> None:
    vendor_dir = resolver.module_dir / "vendor"
    if not vendor_dir.exists():
        log.info("vendoring dependencies")
        resolver.vendor_deps()

    log.info("identifying vendored dependencies")
    vendor = get_names_and_versions(resolver.parse_vendor())
    vendor_with_unused = get_names_and_versions(resolver.parse_vendor(drop_unused=False))

    _write_results(vendor, output_dir / "vendor.txt")
    _write_results(vendor_with_unused, output_dir / "vendor_with_unused.txt")

    if vendor_diff := _diff_vendor_modules(vendor, vendor_dir):
        log.info("diffing vendor dirs: identified x actual")
        print(vendor_diff)
    else:
        log.info("diffing vendor dirs: perfect match")


def _write_results(results: list[NameVersion], filepath: Path) -> None:
    log.info("writing %s", filepath)
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with filepath.open("w") as f:
        print("\n".join(map(str, results)), file=f)


def _get_diff(left: Iterable[str], right: Iterable[str]) -> str:
    return "\n".join(difflib.unified_diff(sorted(left), sorted(right), lineterm=""))


def _diff_vendor_modules(vendor_modules: Iterable[NameVersion], vendor_dir: Path) -> str:
    identified_vendor_dirs = {Path(module_name) for module_name, _ in vendor_modules}

    def find_unknown_vendor_dirs(vendor_subdir: Path) -> Iterator[Path]:
        relpath_from_vendor = vendor_subdir.relative_to(vendor_dir)
        if any(
            relpath_from_vendor.is_relative_to(known_path) for known_path in identified_vendor_dirs
        ):
            return
        child_paths = list(vendor_subdir.iterdir())
        if any(child_path.is_file() for child_path in child_paths):
            yield relpath_from_vendor
        else:
            for child_dir in filter(Path.is_dir, child_paths):
                yield from find_unknown_vendor_dirs(child_dir)

    actual_vendor_dirs = {p for p in identified_vendor_dirs if vendor_dir.joinpath(p).exists()}
    actual_vendor_dirs.update(
        unknown_dir
        for vendor_subdir in filter(Path.is_dir, vendor_dir.iterdir())
        for unknown_dir in find_unknown_vendor_dirs(vendor_subdir)
    )

    return _get_diff(map(str, identified_vendor_dirs), map(str, actual_vendor_dirs))


def _print_deptree(resolver: GomodResolver):
    depstack: list[set[str]] = []

    for package in reversed(resolver.parse_list_deps()):
        if not package.module:
            continue

        name = package.import_path
        version = "main" if package.module.main else NameVersion.from_module(package.module).version

        while depstack and name not in depstack[-1]:
            depstack.pop()

        print(f"{' ' * 4 * len(depstack)}{name}@{version}")
        depstack.append(set(package.deps))


if __name__ == "__main__":
    main()
