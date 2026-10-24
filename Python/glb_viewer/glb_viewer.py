"""
GLB Viewer (PySide6 + PyVista)

インストール:
    pip install pyvista pyvistaqt PySide6

起動:
    python glb_viewer.py
    python glb_viewer.py model.glb

操作:
    左ドラッグ: 回転 / 右ドラッグ or ホイール: ズーム / 中ドラッグ: 平行移動
    ファイルはウィンドウへのドラッグ&ドロップでも開けます

Blender連携:
    glTF 1.0 のモデルは Node.js の gltf-pipeline で 2.0 に変換してから表示します(Node.js が必要)。
    VTKが対応していない glTF拡張(Draco圧縮 / WebPテクスチャ など)を含むモデルは、
    指定したBlenderで一度GLBに変換し直してから表示します。
    「設定 > Blenderフォルダを指定...」で Blender のインストールフォルダ
    (例: C:\\Program Files\\Blender Foundation\\Blender 4.2)を指定してください。
"""
import hashlib
import json
import os
import shutil
import struct
import subprocess
import sys
import tempfile

os.environ["QT_API"] = "pyside6"  # pyvistaqt/QtPyにPySide6を使わせる(他のimportより前)

import pyvista as pv
from pyvistaqt import QtInteractor
from PySide6.QtCore import QSettings, Qt
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QDockWidget,
    QFileDialog,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

# VTKのglTFローダが「必須拡張」として扱えないもの
UNSUPPORTED_EXTENSIONS = {
    "KHR_draco_mesh_compression",
    "EXT_texture_webp",
    "EXT_meshopt_compression",
    "KHR_texture_basisu",
}

APP_TITLE = "GLB Viewer(日本語フォルダ未対応)"

CACHE_DIR = os.path.join(tempfile.gettempdir(), "glb_viewer_cache")

# Blender内で実行する変換スクリプト
BLENDER_SCRIPT = """
import bpy, sys
argv = sys.argv
src, dst = argv[argv.index("--") + 1:][:2]
bpy.ops.wm.read_factory_settings(use_empty=True)
bpy.ops.import_scene.gltf(filepath=src)
# WebPテクスチャはPNGとして書き出させる
for img in bpy.data.images:
    try:
        if img.file_format == "WEBP":
            img.file_format = "PNG"
    except Exception:
        pass
bpy.ops.export_scene.gltf(filepath=dst, export_format="GLB", export_image_format="AUTO")
"""


def read_required_extensions(path):
    """GLB/glTF の JSON 部分から extensionsRequired を読む。失敗したら空集合。"""
    try:
        with open(path, "rb") as f:
            head = f.read(4)
            if head == b"glTF":
                f.read(8)  # version, length
                chunk_len, chunk_type = struct.unpack("<II", f.read(8))
                if chunk_type != 0x4E4F534A:  # 'JSON'
                    return set()
                data = json.loads(f.read(chunk_len).decode("utf-8"))
            else:
                f.seek(0)
                data = json.loads(f.read().decode("utf-8"))
        return set(data.get("extensionsRequired", []))
    except Exception:  # noqa: BLE001
        return set()


def gltf_major_version(path):
    """glTFのメジャーバージョンを返す(判定できなければ2)。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
            if head[:4] == b"glTF":
                return struct.unpack("<I", head[4:8])[0]
            f.seek(0)
            data = json.loads(f.read().decode("utf-8"))
        return int(str(data.get("asset", {}).get("version", "2")).split(".")[0])
    except Exception:  # noqa: BLE001
        return 2


def find_blender_exe(path):
    """指定されたフォルダ(またはexe)から blender.exe を探す。"""
    if not path:
        return None
    names = ("blender.exe", "blender")
    if os.path.isfile(path):
        return path if os.path.basename(path).lower() in names else None
    if not os.path.isdir(path):
        return None
    for n in names:
        p = os.path.join(path, n)
        if os.path.isfile(p):
            return p
    # 1階層下も探す(Blender Foundation フォルダを指定した場合など)
    try:
        for sub in sorted(os.listdir(path), reverse=True):
            for n in names:
                p = os.path.join(path, sub, n)
                if os.path.isfile(p):
                    return p
    except OSError:
        pass
    return None


class GlbViewer(QMainWindow):
    def __init__(self, path=None):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1200, 800)
        self.setAcceptDrops(True)

        self.settings = QSettings("VDSP", "GlbViewer")
        self.current_path = None
        self.origin_actor = None
        self.model_bounds = None
        self.n_actors = 0

        # --- 3D表示領域 ---
        self.plotter = QtInteractor(self)
        self.setCentralWidget(self.plotter.interactor)
        self.plotter.set_background("#2b2b2b")
        self.plotter.add_axes()

        self._build_menu()
        self._build_side_panel()
        self.update_blender_label()

        self.statusBar().showMessage("GLBファイルを開くか、ここにドラッグ&ドロップしてください")

        if path:
            self.load_glb(path)

    # ------------------------------------------------------------ UI構築
    def _build_menu(self):
        file_menu = self.menuBar().addMenu("ファイル(&F)")

        act_open = QAction("開く...", self)
        act_open.setShortcut(QKeySequence.Open)
        act_open.triggered.connect(self.open_dialog)
        file_menu.addAction(act_open)

        act_reload = QAction("再読み込み", self)
        act_reload.setShortcut("F5")
        act_reload.triggered.connect(self.reload)
        file_menu.addAction(act_reload)

        act_shot = QAction("スクリーンショット保存...", self)
        act_shot.setShortcut("Ctrl+S")
        act_shot.triggered.connect(self.save_screenshot)
        file_menu.addAction(act_shot)

        file_menu.addSeparator()
        act_quit = QAction("終了", self)
        act_quit.triggered.connect(self.close)
        file_menu.addAction(act_quit)

        view_menu = self.menuBar().addMenu("表示(&V)")
        for label, fn in [
            ("等角ビュー", self.plotter.view_isometric),
            ("正面 (+Y方向から)", self.plotter.view_xz),
            ("側面 (+X方向から)", self.plotter.view_yz),
            ("上面 (+Z方向から)", self.plotter.view_xy),
        ]:
            a = QAction(label, self)
            a.triggered.connect(lambda _=False, f=fn: f())
            view_menu.addAction(a)

        set_menu = self.menuBar().addMenu("設定(&S)")
        act_blender = QAction("Blenderフォルダを指定...", self)
        act_blender.triggered.connect(self.pick_blender_dir)
        set_menu.addAction(act_blender)

        act_clear = QAction("Blender変換キャッシュを削除", self)
        act_clear.triggered.connect(self.clear_cache)
        set_menu.addAction(act_clear)

    def _build_side_panel(self):
        panel = QWidget()
        lay = QVBoxLayout(panel)
        lay.setAlignment(Qt.AlignTop)

        btn_open = QPushButton("ファイルを開く...")
        btn_open.clicked.connect(self.open_dialog)
        lay.addWidget(btn_open)

        self.chk_wire = QCheckBox("ワイヤーフレーム")
        self.chk_wire.toggled.connect(self.apply_style)
        lay.addWidget(self.chk_wire)

        self.chk_axes = QCheckBox("軸を表示")
        self.chk_axes.setChecked(True)
        self.chk_axes.toggled.connect(self.toggle_axes)
        lay.addWidget(self.chk_axes)

        self.chk_grid = QCheckBox("グリッド(バウンディングボックス)")
        self.chk_grid.toggled.connect(self.toggle_grid)
        lay.addWidget(self.chk_grid)

        self.chk_origin = QCheckBox("原点マーカー(X赤 / Y緑 / Z青)")
        self.chk_origin.toggled.connect(self.toggle_origin)
        lay.addWidget(self.chk_origin)

        btn_reset = QPushButton("カメラリセット")
        btn_reset.clicked.connect(self.reset_camera)
        lay.addWidget(btn_reset)

        btn_bg = QPushButton("背景色を変更...")
        btn_bg.clicked.connect(self.pick_bg)
        lay.addWidget(btn_bg)

        self.chk_alpha = QCheckBox("スクリーンショットの背景を透過(PNG)")
        self.chk_alpha.setChecked(True)
        lay.addWidget(self.chk_alpha)

        btn_shot = QPushButton("スクリーンショット保存...")
        btn_shot.clicked.connect(self.save_screenshot)
        lay.addWidget(btn_shot)

        lay.addSpacing(12)
        self.chk_auto = QCheckBox("非対応モデルはBlenderで自動変換")
        self.chk_auto.setChecked(self.settings.value("auto_convert", True, type=bool))
        self.chk_auto.toggled.connect(lambda v: self.settings.setValue("auto_convert", v))
        lay.addWidget(self.chk_auto)

        self.chk_force = QCheckBox("常にBlender経由で開く(強制)")
        self.chk_force.setChecked(self.settings.value("force_blender", False, type=bool))
        self.chk_force.toggled.connect(lambda v: self.settings.setValue("force_blender", v))
        lay.addWidget(self.chk_force)

        btn_blender = QPushButton("Blenderフォルダを指定...")
        btn_blender.clicked.connect(self.pick_blender_dir)
        lay.addWidget(btn_blender)

        self.lbl_blender = QLabel("")
        self.lbl_blender.setWordWrap(True)
        self.lbl_blender.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.lbl_blender)

        lay.addSpacing(12)
        self.info = QLabel("(未読み込み)")
        self.info.setWordWrap(True)
        self.info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.info)

        dock = QDockWidget("コントロール", self)
        dock.setWidget(panel)
        dock.setFeatures(QDockWidget.DockWidgetMovable | QDockWidget.DockWidgetFloatable)
        self.addDockWidget(Qt.RightDockWidgetArea, dock)

    # ------------------------------------------------------------ Blender設定
    def blender_exe(self):
        return find_blender_exe(self.settings.value("blender_path", "", type=str))

    def update_blender_label(self):
        exe = self.blender_exe()
        if exe:
            self.lbl_blender.setText(f"Blender: {exe}")
        else:
            self.lbl_blender.setText("Blender: 未設定")

    def pick_blender_dir(self):
        start = self.settings.value("blender_path", "", type=str) or r"C:\Program Files"
        d = QFileDialog.getExistingDirectory(self, "Blenderのインストールフォルダを選択", start)
        if not d:
            return
        exe = find_blender_exe(d)
        if not exe:
            QMessageBox.warning(
                self,
                "見つかりません",
                f"このフォルダに blender.exe が見つかりませんでした:\n{d}\n\n"
                "blender.exe があるフォルダ(例: ...\\Blender 4.2)を指定してください。",
            )
            return
        self.settings.setValue("blender_path", d)
        self.update_blender_label()
        self.statusBar().showMessage(f"Blenderを設定しました: {exe}")

    def clear_cache(self):
        n = 0
        if os.path.isdir(CACHE_DIR):
            for name in os.listdir(CACHE_DIR):
                try:
                    os.remove(os.path.join(CACHE_DIR, name))
                    n += 1
                except OSError:
                    pass
        self.statusBar().showMessage(f"キャッシュを削除しました ({n}件)")

    def convert_with_blender(self, src):
        """Blenderで src を読み直して GLB として書き出し、そのパスを返す。"""
        exe = self.blender_exe()
        if not exe:
            raise RuntimeError("Blenderが未設定です。「設定 > Blenderフォルダを指定...」で指定してください。")

        os.makedirs(CACHE_DIR, exist_ok=True)
        key = f"{os.path.abspath(src)}|{os.path.getmtime(src)}"
        name = hashlib.md5(key.encode("utf-8")).hexdigest()
        dst = os.path.join(CACHE_DIR, name + ".glb")
        if os.path.isfile(dst):
            return dst  # 変換済みキャッシュ

        script = os.path.join(CACHE_DIR, "convert.py")
        with open(script, "w", encoding="utf-8") as f:
            f.write(BLENDER_SCRIPT)

        cmd = [exe, "-b", "--factory-startup", "--python", script, "--", src, dst]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        self.statusBar().showMessage("Blenderで変換中...(数十秒かかることがあります)")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                creationflags=flags,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("Blenderの変換がタイムアウトしました。")
        finally:
            QApplication.restoreOverrideCursor()

        if r.returncode != 0 or not os.path.isfile(dst):
            tail = (r.stdout + "\n" + r.stderr).strip()[-800:]
            raise RuntimeError(f"Blenderでの変換に失敗しました。\n\n{tail}")
        return dst

    def notify_old_format(self, path):
        """glTF 1.0 検出時のお知らせ(「今後表示しない」で抑止可能)。"""
        if self.settings.value("hide_old_notice", False, type=bool):
            return
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Warning)
        box.setWindowTitle("形式が古いモデルです")
        box.setText(
            f"{os.path.basename(path)} は glTF 1.0 (古い形式)です。\n\n"
            "このビューアは glTF 2.0 で表示するため、自動で 2.0 に変換します。\n"
            "見た目(色・テクスチャ・質感)が元と違って表示されることがあります。"
        )
        cb = QCheckBox("今後このメッセージを表示しない")
        box.setCheckBox(cb)
        box.setStandardButtons(QMessageBox.Ok)
        box.exec()
        if cb.isChecked():
            self.settings.setValue("hide_old_notice", True)

    def upgrade_gltf1(self, src):
        """glTF 1.0 を gltf-pipeline(Node.js)で 2.0 のGLBに変換し、そのパスを返す。"""
        os.makedirs(CACHE_DIR, exist_ok=True)
        key = f"v1|{os.path.abspath(src)}|{os.path.getmtime(src)}"
        dst = os.path.join(CACHE_DIR, hashlib.md5(key.encode("utf-8")).hexdigest() + "_v2.glb")
        if os.path.isfile(dst):
            return dst

        npx = shutil.which("npx")
        if not npx:
            raise RuntimeError(
                "このモデルは glTF 1.0 (古い形式)です。\n"
                "2.0 への変換には Node.js が必要です。\n"
                "https://nodejs.org/ からインストール後、もう一度開いてください。"
            )

        cmd = [npx, "--yes", "gltf-pipeline", "-i", src, "-o", dst, "-b"]
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0

        self.statusBar().showMessage("glTF 1.0 → 2.0 に変換中...(初回はダウンロードで時間がかかります)")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()
        try:
            r = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=600,
                creationflags=flags,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("glTF 1.0 → 2.0 の変換がタイムアウトしました。")
        finally:
            QApplication.restoreOverrideCursor()

        if r.returncode != 0 or not os.path.isfile(dst):
            tail = (r.stdout + "\n" + r.stderr).strip()[-800:]
            raise RuntimeError(f"glTF 1.0 → 2.0 の変換に失敗しました。\n\n{tail}")
        return dst

    # ------------------------------------------------------------ 読み込み
    def open_dialog(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "GLB/glTFを開く", "", "glTF (*.glb *.gltf);;すべてのファイル (*)"
        )
        if path:
            self.load_glb(path)

    def reload(self):
        if self.current_path:
            self.load_glb(self.current_path)

    def _import(self, path):
        """VTKで読み込み、アクターが1つ以上できたら True。"""
        try:
            self.plotter.clear()
            self.plotter.import_gltf(path)
        except Exception:  # noqa: BLE001
            return False
        return self.plotter.renderer.GetActors().GetNumberOfItems() > 0

    def load_glb(self, path):
        if not os.path.isfile(path):
            QMessageBox.warning(self, "エラー", f"ファイルが見つかりません:\n{path}")
            return

        src = path  # 以降の変換元(glTF 1.0 の場合はアップグレード後のファイル)
        converted = False

        # 0) glTF 1.0 は 2.0 に上げる
        if gltf_major_version(path) < 2:
            self.notify_old_format(path)
            try:
                src = self.upgrade_gltf1(path)
                converted = True
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "glTF 1.0 の変換失敗", str(e))
                return

        load_path = src
        via_blender = False
        unsupported = read_required_extensions(src) & UNSUPPORTED_EXTENSIONS

        # 強制指定: Blenderが使えるなら常にBlender経由にする
        force = self.chk_force.isChecked()
        if force and not self.blender_exe():
            QMessageBox.warning(
                self,
                "Blenderが未設定です",
                "「常にBlender経由で開く」がONですが、Blenderが設定されていません。\n"
                "「設定 > Blenderフォルダを指定...」で指定してください。\n\n"
                "今回はBlenderを使わずに開きます。",
            )
            force = False

        # 1) 強制指定、または非対応拡張が分かっている場合は最初からBlender変換
        if force:
            try:
                load_path = self.convert_with_blender(src)
                converted = True
                via_blender = True
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "変換失敗", str(e))
                return
        elif unsupported:
            if not (self.chk_auto.isChecked() and self.blender_exe()):
                QMessageBox.warning(
                    self,
                    "非対応のモデルです",
                    "このモデルはVTKが対応していない拡張を使っています:\n"
                    + "\n".join(sorted(unsupported))
                    + "\n\n「設定 > Blenderフォルダを指定...」でBlenderを設定し、"
                    "「非対応モデルはBlenderで自動変換」をONにすると表示できます。",
                )
                return
            try:
                load_path = self.convert_with_blender(src)
                converted = True
                via_blender = True
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "変換失敗", str(e))
                return

        ok = self._import(load_path)

        # 2) 拡張は問題なさそうなのに空だった場合も、Blender変換を試す
        if not ok and not via_blender and self.chk_auto.isChecked() and self.blender_exe():
            try:
                load_path = self.convert_with_blender(src)
                converted = True
                ok = self._import(load_path)
            except Exception as e:  # noqa: BLE001
                QMessageBox.critical(self, "変換失敗", str(e))
                return

        if not ok:
            QMessageBox.critical(
                self,
                "読み込み失敗",
                f"{path}\n\nモデルを表示できませんでした。\n"
                "Blenderフォルダを設定すると、変換して表示できる場合があります。",
            )
            return

        self.current_path = path
        # 軸・グリッド・原点マーカーを足す前に、モデル自体の範囲を記録する
        self.origin_actor = None  # plotter.clear() で消えているため
        self.n_actors = self.plotter.renderer.GetActors().GetNumberOfItems()
        self.model_bounds = self.plotter.renderer.ComputeVisiblePropBounds()
        if self.chk_axes.isChecked():
            self.plotter.add_axes()
        if self.chk_grid.isChecked():
            self.plotter.show_bounds(grid="back", location="outer", all_edges=True)
        self.apply_style()
        if self.chk_origin.isChecked():
            self._show_origin()
        self.plotter.reset_camera()
        self.plotter.view_isometric()

        self.setWindowTitle(f"{APP_TITLE} - {os.path.basename(path)}")
        self.update_info(path, converted)
        self.statusBar().showMessage(
            f"読み込み完了: {path}" + (" (変換して表示)" if converted else "")
        )

    def update_info(self, path, converted=False):
        b = self.model_bounds
        size = (b[1] - b[0], b[3] - b[2], b[5] - b[4])
        kb = os.path.getsize(path) / 1024
        text = (
            f"ファイル: {os.path.basename(path)}\n"
            f"サイズ: {kb:,.1f} KB\n"
            f"メッシュ(アクター)数: {self.n_actors}\n"
            f"最小 X/Y/Z:\n  {b[0]:.4g} / {b[2]:.4g} / {b[4]:.4g}\n"
            f"最大 X/Y/Z:\n  {b[1]:.4g} / {b[3]:.4g} / {b[5]:.4g}\n"
            f"寸法 X/Y/Z:\n  {size[0]:.4g} / {size[1]:.4g} / {size[2]:.4g}"
        )
        if converted:
            text += "\n(変換して表示中)"
        if gltf_major_version(path) < 2:
            text += "\n※ 元の形式: glTF 1.0 (古い形式)"
        self.info.setText(text)

    # ------------------------------------------------------------ 表示設定
    def apply_style(self, *_):
        actors = self.plotter.renderer.GetActors()
        actors.InitTraversal()
        for _i in range(actors.GetNumberOfItems()):
            prop = actors.GetNextActor().GetProperty()
            if self.chk_wire.isChecked():
                prop.SetRepresentationToWireframe()
            else:
                prop.SetRepresentationToSurface()
        self.plotter.render()

    def toggle_axes(self, on):
        if on:
            self.plotter.add_axes()
        else:
            self.plotter.hide_axes()
        self.plotter.render()

    def toggle_grid(self, on):
        if on:
            self.plotter.show_bounds(grid="back", location="outer", all_edges=True)
        else:
            self.plotter.remove_bounds_axes()
        self.plotter.render()

    def _hide_origin(self):
        if self.origin_actor is not None:
            self.plotter.remove_actor(self.origin_actor)
            self.origin_actor = None

    def _show_origin(self):
        self._hide_origin()
        b = self.model_bounds
        if not b:
            return
        size = max(b[1] - b[0], b[3] - b[2], b[5] - b[4])
        length = size * 0.3 if size > 0 else 1.0
        self.origin_actor = self.plotter.add_axes_at_origin(line_width=3)
        self.origin_actor.SetTotalLength(length, length, length)

    def toggle_origin(self, on):
        if on:
            self._show_origin()
        else:
            self._hide_origin()
        self.plotter.render()

    def reset_camera(self):
        self.plotter.reset_camera()
        self.plotter.view_isometric()

    def pick_bg(self):
        color = QColorDialog.getColor(parent=self, title="背景色")
        if color.isValid():
            self.plotter.set_background(color.name())
            self.plotter.render()

    def save_screenshot(self):
        path, _ = QFileDialog.getSaveFileName(
            self, "スクリーンショット保存", "screenshot.png", "PNG (*.png);;JPEG (*.jpg)"
        )
        if not path:
            return

        alpha = self.chk_alpha.isChecked() and path.lower().endswith(".png")

        # 透過時は軸ウィジェットやグリッドも写り込むので、撮影中だけ隠す
        if alpha:
            self.plotter.hide_axes()
            self.plotter.remove_bounds_axes()
            if self.origin_actor is not None:
                self.origin_actor.SetVisibility(False)

        self.plotter.screenshot(path, transparent_background=alpha)

        if alpha:
            if self.origin_actor is not None:
                self.origin_actor.SetVisibility(True)
            if self.chk_axes.isChecked():
                self.plotter.add_axes()
            if self.chk_grid.isChecked():
                self.plotter.show_bounds(grid="back", location="outer", all_edges=True)
            self.plotter.render()

        self.statusBar().showMessage(f"保存しました: {path}")

    # ------------------------------------------------------------ D&D
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p.lower().endswith((".glb", ".gltf")):
                self.load_glb(p)
                break

    def closeEvent(self, e):
        self.plotter.close()
        super().closeEvent(e)


def main():
    app = QApplication(sys.argv)
    path = sys.argv[1] if len(sys.argv) > 1 else None
    win = GlbViewer(path)
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
