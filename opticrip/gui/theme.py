"""Dark theme QSS stylesheet (Phase 7).

Kept as pure data so it can be loadtested without a QApplication.
"""
QSS = """
* { background-color: #1e1f24; color: #e3e4e8; font-family: 'Inter','DejaVu Sans',sans-serif; font-size: 13px; }
QMainWindow, QDialog { background-color: #17181c; }
#header { background-color: #17181c; }
#header QLabel#title { font-size: 18px; font-weight: 700; color: #7cc4ff; }
QGroupBox { border: 1px solid #34363d; border-radius: 8px; margin-top: 12px; padding: 8px; }
QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; color: #9aa0ab; }
QLineEdit, QSpinBox, QComboBox { background-color: #26272d; border: 1px solid #3a3c44; border-radius: 6px; padding: 4px 6px; }
QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color: #7cc4ff; }
QPushButton { background-color: #2b6cb0; border: none; border-radius: 6px; padding: 6px 14px; font-weight: 600; color: white; }
QPushButton:disabled { background-color: #3a3c44; color: #77797f; }
QPushButton#secondary { background-color: #3a3c44; }
QPushButton#danger { background-color: #b02b2b; }
QTreeWidget, QListWidget, QPlainTextEdit { background-color: #202127; border: 1px solid #34363d; border-radius: 6px; }
QHeaderView::section { background-color: #26272d; border: none; padding: 4px; }
QProgressBar { background-color: #26272d; border: 1px solid #3a3c44; border-radius: 6px; text-align: center; height: 16px; }
QProgressBar::chunk { background-color: #2f9e6e; border-radius: 5px; }
QStatusBar { background-color: #17181c; color: #9aa0ab; }
"""
