from app.services.telegram_listener import build_route_keys_for_identifier, resolve_target_id


def test_build_route_keys_for_username_identifier():
    keys = build_route_keys_for_identifier("https://t.me/SomeChannel")
    assert "@somechannel" in keys
    assert "somechannel" in keys


def test_build_route_keys_for_signed_numeric_identifier():
    keys = build_route_keys_for_identifier("-1001234567890")
    assert "-1001234567890" in keys
    assert "1234567890" in keys


def test_resolve_target_id_uses_normalized_keys():
    route_map = {"@ukrdropcard": 11, "-1001234567890": 22}
    assert resolve_target_id(route_map, {"@ukrdropcard"}) == 11
    assert resolve_target_id(route_map, {"-1001234567890"}) == 22
    assert resolve_target_id(route_map, {"123"}) is None

