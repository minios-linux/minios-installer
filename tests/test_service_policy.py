from install_state import set_service_enabled


def test_security_page_service_toggle_edits_generic_service_fields():
    enable, disable = set_service_enabled("cups", "ssh,xrdp", "ssh", True)
    assert enable == "cups,ssh"
    assert disable == "xrdp"


def test_security_page_disabled_service_does_not_add_disable_override():
    enable, disable = set_service_enabled("cups,ssh", "bluetooth", "ssh", False)
    assert enable == "cups"
    assert disable == "bluetooth"
