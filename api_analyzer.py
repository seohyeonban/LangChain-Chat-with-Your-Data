import argparse
import ast
import importlib.util
import json
import shutil
import tarfile
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

import nbformat


TARGET_PACKAGES = (
    "langchain",
    "langchain_community",
    "langchain_core",
    "langchain_openai",
    "langchain_text_splitters",
)


PYPI_PROJECTS = {
    "langchain": "langchain",
    "langchain_community": "langchain-community",
    "langchain_core": "langchain-core",
    "langchain_openai": "langchain-openai",
    "langchain_text_splitters": "langchain-text-splitters",
}

PYPI_URL = "https://pypi.org/pypi/{package}/json"
CACHE_DIR = Path(tempfile.gettempdir()) / "langchain_latest_api_cache"


@dataclass
class PackageIndex:
    package: str
    version: str = ""
    names: set = field(default_factory=set)
    names_by_leaf: dict = field(default_factory=dict)
    available: bool = False
    error: str = ""

    def add(self, qualified_name):
        self.names.add(qualified_name)
        leaf = qualified_name.rsplit(".", 1)[-1]
        self.names_by_leaf.setdefault(leaf, set()).add(qualified_name)


class NotebookLoader:
    @staticmethod
    def load(path):
        if path.suffix.lower() != ".ipynb":
            raise ValueError(".ipynb 파일만 분석할 수 있습니다.")

        notebook = nbformat.read(path, as_version=4)
        return [
            (index, cell.source or "")
            for index, cell in enumerate(notebook.cells)
            if cell.cell_type == "code"
        ]

    @staticmethod
    def remove_jupyter_commands(source):
        lines = []
        for line in source.splitlines():
            if line.lstrip().startswith(("%", "!", "?")):
                lines.append("")
            else:
                lines.append(line)
        return "\n".join(lines)


class APIExtractor(ast.NodeVisitor):
    """Import 별칭과 객체 할당을 추적해 LangChain FQN을 추출한다."""

    def __init__(self, cell, source, symbols):
        self.cell = cell
        self.source = source
        self.symbols = symbols
        self.results = []

    def add(self, node, kind, qualified_name):
        if not self.is_target(qualified_name):
            return
        self.results.append(
            {
                "cell": self.cell,
                "line": getattr(node, "lineno", None),
                "type": kind,
                "code": ast.get_source_segment(self.source, node),
                "api": qualified_name,
            }
        )

    @staticmethod
    def is_target(name):
        return bool(name) and name.split(".", 1)[0] in TARGET_PACKAGES

    def resolve(self, node):
        if isinstance(node, ast.Name):
            return self.symbols.get(node.id)

        if isinstance(node, ast.Attribute):
            parent = self.resolve(node.value)
            return f"{parent}.{node.attr}" if parent else None

        if isinstance(node, ast.Call):
            return self.resolve(node.func)

        return None

    def visit_Import(self, node):
        for alias in node.names:
            if alias.asname:
                self.symbols[alias.asname] = alias.name
            else:
                root = alias.name.split(".", 1)[0]
                self.symbols[root] = root
            self.add(node, "IMPORT", alias.name)

    def visit_ImportFrom(self, node):
        module = node.module or ""
        for alias in node.names:
            if alias.name == "*":
                continue
            qualified_name = f"{module}.{alias.name}" if module else alias.name
            self.symbols[alias.asname or alias.name] = qualified_name
            self.add(node, "IMPORT", qualified_name)

    def visit_Assign(self, node):
        value_name = self.resolve(node.value)
        for target in node.targets:
            if isinstance(target, ast.Name):
                if value_name:
                    self.symbols[target.id] = value_name
                else:
                    self.symbols.pop(target.id, None)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if isinstance(node.target, ast.Name):
            value_name = self.resolve(node.value) if node.value else None
            if value_name:
                self.symbols[node.target.id] = value_name
            else:
                self.symbols.pop(node.target.id, None)
        self.generic_visit(node)

    def visit_FunctionDef(self, node):
        outer_symbols = self.symbols.copy()
        self.generic_visit(node)
        self.symbols.clear()
        self.symbols.update(outer_symbols)

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        outer_symbols = self.symbols.copy()
        self.generic_visit(node)
        self.symbols.clear()
        self.symbols.update(outer_symbols)

    def visit_Call(self, node):
        qualified_name = self.resolve(node.func)
        call_type = "METHOD_CALL" if isinstance(node.func, ast.Attribute) else "API_CALL"
        self.add(node, call_type, qualified_name)
        self.generic_visit(node)


class LatestAPIIndex:
    """PyPI 최신 배포본에서 공개 API와 모듈 경로를 색인한다."""

    def __init__(self):
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self.indexes = {package: self.build(package) for package in TARGET_PACKAGES}

    def build(self, package):
        index = PackageIndex(package)
        try:
            project = PYPI_PROJECTS[package]
            metadata = self.read_json(PYPI_URL.format(package=project))
            index.version = metadata["info"]["version"]
            archive_url, archive_type = self.select_archive(metadata, index.version)
            source_root = self.download_and_extract(package, index.version, archive_url, archive_type)
            package_dir = self.find_package_directory(source_root, package)
            if package_dir is None:
                raise RuntimeError("패키지 소스 디렉터리를 찾지 못했습니다.")
            self.index_directory(package, package_dir, index)
            index.available = True
        except Exception as error:
            index.error = str(error)
        return index

    @staticmethod
    def read_json(url):
        try:
            with urllib.request.urlopen(url, timeout=20) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError) as error:
            raise RuntimeError(f"PyPI 조회 실패: {error}") from error

    @staticmethod
    def select_archive(metadata, version):
        files = metadata.get("releases", {}).get(version, [])
        source = next((item for item in files if item.get("packagetype") == "sdist"), None)
        wheel = next((item for item in files if item.get("packagetype") == "bdist_wheel"), None)
        selected = source or wheel
        if selected is None:
            raise RuntimeError("최신 버전의 배포 파일을 찾지 못했습니다.")
        return selected["url"], selected["packagetype"]

    @staticmethod
    def download_and_extract(package, version, url, archive_type):
        destination = CACHE_DIR / f"{package}-{version}"
        if destination.exists():
            return destination

        destination.mkdir(parents=True)
        archive = destination / Path(url).name
        try:
            urllib.request.urlretrieve(url, archive)
            if archive_type == "sdist":
                with tarfile.open(archive) as file:
                    file.extractall(destination, filter="data")
            else:
                with zipfile.ZipFile(archive) as file:
                    file.extractall(destination)
        except Exception:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        finally:
            if archive.exists():
                archive.unlink()
        return destination

    @staticmethod
    def find_package_directory(root, package):
        package_name = package.replace("-", "_")
        candidates = [
            path
            for path in root.rglob(package_name)
            if path.is_dir() and (path / "__init__.py").exists()
        ]
        return min(candidates, key=lambda path: len(path.parts)) if candidates else None

    def index_directory(self, package, package_dir, index):
        for file in package_dir.rglob("*.py"):
            self.index_file(package, package_dir, file, index)

    @staticmethod
    def index_file(package, package_dir, file, index):
        relative = file.relative_to(package_dir)
        if relative.name == "__init__.py":
            module_parts = relative.parent.parts
        else:
            module_parts = relative.with_suffix("").parts
        module = ".".join((package, *module_parts))

        # import langchain.xxx처럼 모듈 자체를 검사할 수 있도록 모듈도 등록한다.
        index.add(module)

        try:
            tree = ast.parse(file.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, SyntaxError):
            return

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                index.add(f"{module}.{node.name}")
                for member in node.body:
                    if isinstance(member, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        index.add(f"{module}.{node.name}.{member.name}")
            elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.add(f"{module}.{node.name}")
            elif isinstance(node, ast.ImportFrom):
                for alias in node.names:
                    if alias.name != "*":
                        index.add(f"{module}.{alias.asname or alias.name}")

    def exists(self, qualified_name):
        package = qualified_name.split(".", 1)[0]
        index = self.indexes.get(package)
        return bool(index and index.available and qualified_name in index.names)

    def replacements(self, qualified_name):
        leaf = qualified_name.rsplit(".", 1)[-1]
        candidates = set()
        for index in self.indexes.values():
            if index.available:
                candidates.update(index.names_by_leaf.get(leaf, set()))
        candidates.discard(qualified_name)
        return sorted(candidates)


class OutdatedDetector:
    def __init__(self):
        self.latest = LatestAPIIndex()

    def check(self, qualified_name):
        package = qualified_name.split(".", 1)[0]
        index = self.latest.indexes.get(package)

        if index is None:
            return {
                "status": "ANALYSIS_ERROR",
                "replacement": None,
                "reason": f"분석 대상 패키지가 아닙니다: {package}",
            }

        if not index.available:
            return {
                "status": "ANALYSIS_ERROR",
                "replacement": None,
                "reason": index.error,
            }

        if self.latest.exists(qualified_name):
            return {"status": "EXISTS", "replacement": None}

        candidates = self.latest.replacements(qualified_name)
        return {
            "status": "OUTDATED",
            "replacement": candidates[0] if candidates else None,
            "replacement_candidates": candidates,
        }


class NotebookAPIAnalyzer:
    def __init__(self):
        self.detector = OutdatedDetector()

    def analyze(self, notebook_path):
        symbols = {}
        extracted = []
        parse_errors = []

        for cell, source in NotebookLoader.load(notebook_path):
            normalized = NotebookLoader.remove_jupyter_commands(source)
            try:
                tree = ast.parse(normalized)
            except SyntaxError as error:
                parse_errors.append(
                    {"cell": cell, "line": error.lineno, "message": error.msg}
                )
                continue

            extractor = APIExtractor(cell, normalized, symbols)
            extractor.visit(tree)
            extracted.extend(extractor.results)

        # 같은 셀·줄·API가 중복 추출된 경우 한 번만 검사한다.
        unique = {}
        for item in extracted:
            key = (item["cell"], item["line"], item["type"], item["api"])
            unique[key] = item

        results = []
        for item in unique.values():
            item.update(self.detector.check(item["api"]))
            results.append(item)

        summary = {}
        for item in results:
            summary[item["status"]] = summary.get(item["status"], 0) + 1

        return {
            "file": str(notebook_path),
            "comparison_basis": "PyPI에 공개된 각 대상 패키지의 최신 버전",
            "detection_rule": (
                "추출한 import/API 전체 경로가 최신 버전 소스 색인에 없으면 OUTDATED"
            ),
            "latest_versions": {
                package: index.version
                for package, index in self.detector.latest.indexes.items()
            },
            "summary": summary,
            "parse_errors": parse_errors,
            "results": results,
        }


def main():
    parser = argparse.ArgumentParser(
        description="최신 LangChain에 없는 import/API를 탐지합니다."
    )
    parser.add_argument("notebook", type=Path, help="분석할 .ipynb 파일")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("outdated_api_result.json"),
        help="결과 JSON 파일",
    )
    args = parser.parse_args()

    result = NotebookAPIAnalyzer().analyze(args.notebook)
    args.output.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"분석 완료: {args.output}")
    print(json.dumps(result["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
