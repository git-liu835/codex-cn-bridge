"""将 Python 后端打包为独立可执行文件（PyInstaller）"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIST = ROOT / "dist-backend"


def run(cmd: list[str]) -> None:
    subprocess.check_call(cmd)


def main():
    DIST.mkdir(exist_ok=True)

    pip = [sys.executable, "-m", "pip", "install", "-q", "--user", "--disable-pip-version-check"]

    run(pip + ["pyinstaller"])
    run(pip + ["-e", str(ROOT)])

    # 入口脚本
    entry_dir = ROOT / "build"
    entry_dir.mkdir(exist_ok=True)
    entry = entry_dir / "_entry.py"
    entry.write_text("from code_cn_bridge.cli import main; main()", encoding="utf-8")

    # 关键隐式依赖
    hidden = [
        "code_cn_bridge",
        "uvicorn.logging",
        "uvicorn.loops.auto",
        "uvicorn.protocols.http.auto",
        "fastapi",
        "starlette",
        "httpx",
        "click",
        "yaml",
        "watchfiles",
        "pydantic",
    ]

    # 追加 code_cn_bridge 子模块
    for f in sorted(ROOT.glob("code_cn_bridge/**/*.py")):
        if f.name == "__init__.py":
            continue
        rel = f.relative_to(ROOT)
        mod = str(rel.with_suffix("")).replace("/", ".").replace("\\", ".")
        hidden.append(mod)

    cmd = [sys.executable, "-m", "PyInstaller"]
    for m in hidden:
        cmd += ["--hidden-import", m]
    cmd += [
        "--onefile",
        "--noconfirm",
        "--clean",
        "--name", "code-cn-bridge",
        "--distpath", str(DIST),
        "--workpath", str(entry_dir / "pyinstaller"),
        "--specpath", str(entry_dir),
        "--collect-all", "code_cn_bridge",
        str(entry),
    ]

    run(cmd)

    ext = ".exe" if sys.platform == "win32" else ""
    print(f"\nBackend built -> {DIST / f'code-cn-bridge{ext}'}")


if __name__ == "__main__":
    main()
