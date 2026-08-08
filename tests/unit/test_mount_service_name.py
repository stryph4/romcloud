from romcloud.integrations.batocera import mount_service


def test_service_name_is_valid_identifier():
    # Batocera service names must not contain '-' and should be simple
    # identifiers (letters, digits, underscore). Guard against regressions
    # that reintroduce invalid names.
    name = mount_service.SERVICE_NAME
    assert "-" not in name
    assert name.isidentifier()
