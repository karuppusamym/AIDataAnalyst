from aida.workflows.activities import classify_column_name


def test_deterministic_sensitive_name_classification() -> None:
    assert classify_column_name("email_address") == "PII"
    assert classify_column_name("customer_name") == "PII"
    assert classify_column_name("payment_card_number") == "PCI"
    assert classify_column_name("current_balance") == "UNCLASSIFIED"
