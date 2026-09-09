from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ThemePalette:
    mode: str
    app_bg: str
    panel_bg: str
    border: str
    text_primary: str
    text_secondary: str
    accent: str
    accent_hover: str
    warning: str
    danger: str
    success: str
    disabled_bg: str
    disabled_fg: str
    selection_bg: str


DARK_THEME = ThemePalette("dark", "#0A0A0B", "#141416", "#2A2A2D", "#F4F4F5", "#B7B7BC", "#D20F39", "#F03A5F", "#F59E0B", "#C51E3A", "#22C55E", "#202024", "#8C8C93", "#39101A")
LIGHT_THEME = ThemePalette("light", "#ECEDEF", "#FAFAFB", "#D1D3D8", "#1B1F2A", "#4B5161", "#C51E3A", "#D94A63", "#B96A00", "#B71C38", "#1E9A4B", "#E6E8ED", "#7A8090", "#F9DDE4")


def get_theme(mode: str | None) -> ThemePalette:
    return LIGHT_THEME if str(mode or "").casefold() == "light" else DARK_THEME


def build_stylesheet(theme: ThemePalette, ui_scale: float = 1.0) -> str:
    scale = max(0.75, min(2.0, ui_scale))

    def px(value: float) -> int:
        return max(1, round(value * scale))

    def pt(value: float) -> float:
        return max(7.0, round(value * scale, 1))

    return f"""
QMainWindow, QDialog {{ background: {theme.app_bg}; }}
QWidget#trackindexRoot, QWidget#mainColumn {{ background: {theme.app_bg}; }}
QWidget#settingsBody {{ background: transparent; }}
QFrame#card {{ background: {theme.panel_bg}; border: 1px solid {theme.border}; border-radius: {px(8)}px; }}
QFrame#settingsPanel, QFrame#settingsCard, QFrame#modeHolder {{ background: {theme.panel_bg}; border: 2px solid {theme.border}; border-radius: {px(8)}px; }}
QFrame#librarySidebar {{ background: {theme.panel_bg}; border: 1px solid {theme.border}; border-radius: {px(8)}px; }}
QFrame#appFooter {{ background: {theme.app_bg}; border: none; }}
QFrame#authorityBanner {{ background: {theme.selection_bg}; border: 1px solid {theme.warning}; border-radius: {px(7)}px; }}
QFrame#toast {{ background: {theme.panel_bg}; border: 1px solid {theme.border}; border-radius: {px(7)}px; }}
QPushButton#toastAction {{ background: transparent; color: {theme.accent}; border: none; padding: {px(2)}px; font: 700 {pt(9.7):.1f}pt "Segoe UI"; }}
QPushButton#toastAction:hover {{ color: {theme.text_primary}; }}
QFrame#playerPanel {{ background: {theme.panel_bg}; border: 1px solid {theme.border}; border-radius: {px(9)}px; }}
QFrame#audioInfoPopup {{ background: {theme.panel_bg}; border: 1px solid {theme.border}; border-radius: {px(8)}px; }}
QFrame#librarySetupCard {{ background: {theme.panel_bg}; border: 1px solid {theme.border}; border-radius: {px(10)}px; }}
QFrame#setupSummary {{ background: {theme.app_bg}; border: 1px solid {theme.border}; border-radius: {px(7)}px; }}
QWidget#librarySetupPage, QWidget#setupScrollContent, QWidget#trackListPage, QStackedWidget#trackStack {{ background: {theme.app_bg}; border: none; }}
QLabel {{ color: {theme.text_primary}; background: transparent; font-family: "Segoe UI"; font-size: {pt(9.7):.1f}pt; }}
QLabel#title {{ font: 700 {pt(11.0):.1f}pt "Segoe UI"; }}
QLabel#subtitle, QLabel#muted, QLabel#caption {{ color: {theme.text_secondary}; font: 600 {pt(8.4):.1f}pt "Segoe UI"; }}
QLabel#sectionTitle {{ font: 700 {pt(9.4):.1f}pt "Segoe UI"; }}
QLabel#settingsCardTitle {{ color: {theme.text_primary}; font: 700 {pt(9.2):.1f}pt "Segoe UI"; }}
QLabel#settingsSubtext {{ color: {theme.text_secondary}; font: 650 {pt(9.5):.1f}pt "Segoe UI"; padding-top: {px(1)}px; padding-bottom: {px(1)}px; }}
QLabel#playlistCaption {{ color: {theme.text_secondary}; font: 700 {pt(8.4):.1f}pt "Segoe UI"; }}
QLabel#footerVersion {{ color: {theme.text_secondary}; font: 650 {pt(9.2):.1f}pt "Segoe UI"; }}
QLabel#warning {{ color: {theme.warning}; font: 650 {pt(8.8):.1f}pt "Segoe UI"; }}
QLabel#setupSectionLabel {{ color: {theme.text_secondary}; font: 700 {pt(8.4):.1f}pt "Segoe UI"; }}
QLabel#setupHeadingSeparator {{ color: {theme.border}; font: 650 {pt(10.0):.1f}pt "Segoe UI"; }}
QLabel#setupHeadingMeta {{ color: {theme.text_secondary}; font: 650 {pt(9.0):.1f}pt "Segoe UI"; }}
QLabel#setupModeTitle, QLabel#setupSummaryTitle {{ color: {theme.text_primary}; font: 700 {pt(9.2):.1f}pt "Segoe UI"; }}
QLabel#setupModeDescription {{ color: {theme.text_secondary}; font: 600 {pt(8.5):.1f}pt "Segoe UI"; }}
QLabel#setupImpact {{ color: {theme.text_primary}; font: 600 {pt(8.8):.1f}pt "Segoe UI"; padding: 0; }}
QLabel#success {{ color: {theme.success}; font: 650 {pt(8.8):.1f}pt "Segoe UI"; }}
QPushButton {{ background: {theme.panel_bg}; color: {theme.text_primary}; border: 1px solid {theme.border}; border-radius: {px(6)}px; padding: {px(5)}px {px(9)}px; font: 600 {pt(9.1):.1f}pt "Segoe UI"; }}
QPushButton:hover {{ background: {theme.accent}; border-color: {theme.accent}; }}
QPushButton:disabled {{ background: {theme.disabled_bg}; color: {theme.disabled_fg}; border-color: {theme.border}; }}
QFrame#settingsPanel QPushButton#settingsActionButton {{ min-height: {px(32)}px; font: 700 {pt(9.4):.1f}pt "Segoe UI"; }}
QPushButton#primaryButton {{ min-height: {px(36)}px; background: {theme.accent}; border-color: {theme.accent}; font: 700 {pt(10.0):.1f}pt "Segoe UI"; }}
QPushButton#primaryButton:hover {{ background: {theme.accent_hover}; border-color: {theme.accent_hover}; }}
QPushButton#modeButton {{ background: transparent; color: {theme.text_secondary}; border: none; border-radius: {px(5)}px; padding: {px(5)}px {px(10)}px; }}
QPushButton#modeButton:checked {{ background: {theme.accent}; color: {theme.text_primary}; }}
QPushButton#footerLink {{ background: transparent; color: {theme.accent}; border: none; padding: 2px; font: 700 {pt(9.2):.1f}pt "Segoe UI"; text-align: left; }}
QPushButton#footerLink:hover {{ color: {theme.text_primary}; background: transparent; }}
QPushButton#footerIcon {{ background: transparent; color: {theme.text_primary}; border: none; min-width: {px(22)}px; min-height: {px(22)}px; max-width: {px(22)}px; max-height: {px(22)}px; padding: 0; margin: 0; }}
QPushButton#footerIcon:hover {{ background: transparent; }}
QPushButton#playerIconButton {{ background: transparent; border: none; border-radius: {px(13)}px; padding: 0; min-width: {px(26)}px; min-height: {px(26)}px; max-width: {px(26)}px; max-height: {px(26)}px; }}
QPushButton#playerIconButton:hover {{ background: {theme.disabled_bg}; border: none; }}
QPushButton#playerIconButton:checked {{ background: transparent; border: none; }}
QPushButton#playerIconButton:checked:hover {{ background: {theme.disabled_bg}; border: none; }}
QPushButton#playerPrimaryButton {{ background: {theme.accent}; border: none; border-radius: {px(19)}px; padding: 0; min-width: {px(38)}px; min-height: {px(38)}px; max-width: {px(38)}px; max-height: {px(38)}px; }}
QPushButton#playerPrimaryButton:hover {{ background: {theme.accent_hover}; border: none; }}
QPushButton#playerMiniPrimaryButton {{ background: {theme.accent}; border: none; border-radius: {px(16)}px; padding: 0; min-width: {px(32)}px; min-height: {px(32)}px; max-width: {px(32)}px; max-height: {px(32)}px; }}
QPushButton#playerMiniPrimaryButton:hover {{ background: {theme.accent_hover}; border: none; }}
QPushButton#playerUtilityButton {{ background: transparent; border: none; border-radius: {px(13)}px; padding: 0; }}
QPushButton#playerUtilityButton:hover {{ background: {theme.disabled_bg}; border: none; }}
QLabel#playerTrackTitle {{ color: {theme.text_primary}; font: 700 {pt(9.4):.1f}pt "Segoe UI"; padding: 0; }}
QLabel#playerTrackTitle:hover {{ color: {theme.accent_hover}; }}
QLabel#playerTrackMetadata {{ color: {theme.text_secondary}; font: 600 {pt(8.0):.1f}pt "Segoe UI"; padding: 0; }}
QLabel#playerTechnicalLink {{ color: {theme.text_secondary}; font: 600 {pt(7.7):.1f}pt "Segoe UI"; padding: 0; }}
QLabel#playerTechnicalLink:hover {{ color: {theme.accent_hover}; }}
QLabel#playerTime {{ color: {theme.text_secondary}; font: 600 {pt(8.0):.1f}pt "Segoe UI"; padding: 0; }}
QPushButton#libraryActionButton {{ background: {theme.panel_bg}; color: {theme.text_primary}; border: 1px solid {theme.border}; border-radius: {px(17)}px; min-height: {px(28)}px; padding: {px(3)}px {px(12)}px; font: 700 {pt(9.1):.1f}pt "Segoe UI"; }}
QPushButton#libraryActionButton:hover {{ background: {theme.accent}; border-color: {theme.accent}; color: {theme.text_primary}; }}
QPushButton#libraryActionButton:disabled {{ background: {theme.disabled_bg}; color: {theme.disabled_fg}; border-color: {theme.border}; }}
QPushButton#setupChoiceButton {{ min-height: {px(30)}px; background: {theme.app_bg}; }}
QPushButton#setupChoiceButton:checked {{ background: {theme.selection_bg}; border-color: {theme.accent}; }}
QPushButton#setupModeCard {{ min-height: {px(104)}px; padding: 0; background: {theme.app_bg}; border: 1px solid {theme.border}; border-radius: {px(8)}px; text-align: left; }}
QPushButton#setupModeCard:hover {{ background: {theme.selection_bg}; border-color: {theme.accent_hover}; }}
QPushButton#setupModeCard:checked {{ background: {theme.selection_bg}; border-color: {theme.accent}; }}
QPushButton#setupModeCard:focus {{ border-color: {theme.accent}; }}
QLineEdit, QComboBox, QPlainTextEdit, QTextBrowser, QListWidget, QTableView {{ background: {theme.app_bg}; color: {theme.text_primary}; border: 1px solid {theme.border}; border-radius: {px(6)}px; padding: {px(4)}px {px(7)}px; selection-background-color: {theme.accent}; font: 600 {pt(9.0):.1f}pt "Segoe UI"; }}
QLineEdit#settingsReadOnlyInput {{ background: {theme.app_bg}; color: {theme.text_primary}; border: 1px solid {theme.border}; border-radius: {px(6)}px; padding: {px(3)}px {px(7)}px; font: 600 {pt(9.5):.1f}pt "Segoe UI"; }}
QLineEdit#settingsReadOnlyInput:disabled {{ background: {theme.disabled_bg}; color: {theme.disabled_fg}; border-color: {theme.border}; }}
QComboBox::drop-down {{ border: none; width: {px(24)}px; }}
QComboBox QAbstractItemView {{ background: {theme.panel_bg}; color: {theme.text_primary}; border: 1px solid {theme.border}; selection-background-color: {theme.accent}; }}
QListWidget {{ outline: none; padding: {px(5)}px; }}
QTableView {{ outline: none; gridline-color: {theme.border}; selection-background-color: transparent; }}
QHeaderView::section {{ background: {theme.panel_bg}; color: {theme.text_secondary}; border: none; border-bottom: 1px solid {theme.border}; padding: {px(7)}px; font: 700 {pt(8.4):.1f}pt "Segoe UI"; }}
QTableView#trackTable {{ background: {theme.app_bg}; border: none; border-radius: 0; padding: 0; gridline-color: transparent; selection-background-color: transparent; }}
QHeaderView#trackHeader {{ background: transparent; border: none; }}
QHeaderView#trackHeader::section {{ background: transparent; color: {theme.text_secondary}; border: none; padding: {px(5)}px {px(7)}px; font: 700 {pt(8.4):.1f}pt "Segoe UI"; }}
QMenu {{ background: {theme.panel_bg}; color: {theme.text_primary}; border: 1px solid {theme.border}; padding: {px(5)}px; }}
QMenu::item {{ padding: {px(6)}px {px(22)}px; border-radius: {px(4)}px; }}
QMenu::item:selected {{ background: {theme.accent}; }}
QScrollArea {{ background: transparent; border: none; }}
QScrollArea#settingsScroll QWidget#qt_scrollarea_viewport {{ background: transparent; border: none; }}
QScrollArea#setupScroll QWidget#qt_scrollarea_viewport {{ background: {theme.app_bg}; border: none; }}
QCheckBox {{ color: {theme.text_primary}; spacing: {px(8)}px; font: 600 {pt(9.1):.1f}pt "Segoe UI"; }}
QProgressBar {{ background: {theme.app_bg}; color: {theme.text_primary}; border: 1px solid {theme.border}; border-radius: {px(5)}px; text-align: center; font-weight: 700; }}
QProgressBar::chunk {{ background: {theme.accent}; border-radius: {px(4)}px; }}
QMessageBox, QMessageBox QLabel {{ background: {theme.panel_bg}; color: {theme.text_primary}; }}
"""
