# PyInstaller spec for the desktop app's server sidecar: `uv run --group bundle pyinstaller
# desktop/sidecar/mallard-gui-server.spec` (desktop/package.sh does this).
from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_submodules, copy_metadata

datas, binaries, hiddenimports = [], [], collect_submodules("mallard_gui")
# The package's sources too: remote viewers ship them (viewer/k8s.py, viewer/parallel.py).
datas += collect_data_files("mallard_gui", include_py_files=True)
# The server logs these versions at start (ui/app.py).
for dist in ("mallard-gui", "trame-vtk", "vtk"):
    datas += copy_metadata(dist)
for package in ("trame", "trame_client", "trame_common", "trame_server", "trame_vtk", "wslink"):
    d, b, h = collect_all(package)
    datas, binaries, hiddenimports = datas + d, binaries + b, hiddenimports + h
# VTK modules that register object-factory overrides the sources never import by name (server-side text).
hiddenimports += ["vtkmodules.vtkRenderingFreeType", "vtkmodules.vtkRenderingUI"]

a = Analysis(
    ["server.py"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "playwright", "mypy", "ruff", "IPython"],
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="mallard-gui-server",
    console=True,
    strip=False,
    upx=False,
)
coll = COLLECT(exe, a.binaries, a.datas, name="mallard-gui-server", strip=False, upx=False)
