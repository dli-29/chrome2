"""The VPN / Proxy panel stays open when focus moves away and the shield button toggles it."""
from __future__ import annotations

from PyQt6.QtTest import QTest

from helpers import spin


def test_vpn_panel_stays_until_the_shield_is_clicked_again(window, fg):
    window.show()
    window.vpn_button.click()
    panel = window.findChild(fg.VpnPanel)
    assert panel is not None and panel.isVisible()
    assert not panel.isWindow()  # inside the window, not a popup that closes on focus loss

    other = fg.QWidget(None)  # focus moves to another window
    other.show()
    other.activateWindow()
    window.resize(window.width() - 60, window.height())
    spin(0.3)
    assert panel.isVisible()
    assert panel.geometry().right() <= window.width()  # still beside the shield after the resize
    other.close()

    window.act_vpn.trigger()  # the menu item brings it forward, never closes it
    assert panel.isVisible()
    window.vpn_button.click()  # the shield closes it
    spin(0.1)
    assert window.findChild(fg.VpnPanel) is None or not window.findChild(fg.VpnPanel).isVisible()

    window.vpn_button.click()  # and opens it again; Esc closes it too
    panel = window.findChild(fg.VpnPanel)
    assert panel is not None and panel.isVisible()
    QTest.keyClick(panel, fg.Qt.Key.Key_Escape)
    spin(0.1)
    assert not any(p.isVisible() for p in window.findChildren(fg.VpnPanel) if not fg.sip.isdeleted(p))
