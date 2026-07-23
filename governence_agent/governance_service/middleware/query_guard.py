# Tool allowlist per topic.
# Only high-level semantic tools are exposed — raw get_data_from_*_table tools
# are excluded because the high-level tools are already pre-defined query
# templates that cover all customer-facing use cases.
TOPIC_TOOL_ALLOWLIST: dict[str, set[str]] = {
    "SHIPMENT": {
        "get_shipping_and_tracking_details_using_order_number",
        "get_shipping_tracking_details_using_shipment_number",
    },
    "PRODUCT": {
        "get_product_details_in_order",
    },
    "ORDER": {
        "get_order_level_details",
        "get_customer_orders_data",
        "get_customer_order_total_value",
        "get_product_details_in_order",
    },
    "CONTACT": {
        "get_data_from_contact_table",
        "get_data_from_address_table",
    },
}

ACCOUNT_SCOPED_TOOLS: set[str] = {
    "get_customer_orders_data",
    "get_customer_order_total_value",
    "get_data_from_contact_table",
    "get_data_from_address_table",
}

# All tools registered in the real SQL agent (for reference / blocked-tool display)
ALL_TOOLS: set[str] = {
    "get_data_from_customer_table",
    "get_data_from_sales_order_table",
    "get_data_from_contact_table",
    "get_data_from_address_table",
    "get_data_from_sales_order_line_table",
    "get_shipping_and_tracking_details_using_order_number",
    "get_order_address_details",
    "get_data_from_baccount_table",
    "get_customer_orders_data",
    "get_order_level_details",
    "get_product_details_in_order",
    "get_shipping_tracking_details_using_shipment_number",
    "get_customer_order_total_value",
}


def check_tool(tool_name: str, scope_context) -> bool:
    """Return True if tool_name matches the current topic allowlist.

    This is advisory only. Query execution should not be blocked solely
    because the middleware topic guess and the SQL/tool-selection layer
    disagree.
    """
    if scope_context is None:
        return True
    allowed = TOPIC_TOOL_ALLOWLIST.get(scope_context.topic, set())
    return tool_name in allowed


def apply_presentation_policy(tool_name: str, raw_result: str, scope_context) -> tuple[str, str]:
    """Filter post-query output without changing retrieval behavior."""
    if tool_name not in ACCOUNT_SCOPED_TOOLS:
        return raw_result, "no post-query filter"

    if scope_context and scope_context.customer_id:
        return raw_result, "scope verified"

    return (
        "A customer or account identifier is required before showing "
        "account-scoped results.",
        "filtered: missing customer scope",
    )


def inject_scope(tool_name: str, tool_args: dict, scope_context) -> dict:
    """Merge session customer_id into tool_args before ainvoke.

    Only enriches args when scope_context carries a customer_id.
    The tool itself is never modified.
    """
    if not scope_context or not scope_context.customer_id:
        return tool_args

    args = dict(tool_args)
    cid = scope_context.customer_id

    if tool_name == "get_customer_orders_data":
        if not args.get("value"):
            args["value"] = cid
        if not args.get("get_data_by"):
            args["get_data_by"] = "acctCd"

    elif tool_name == "get_customer_order_total_value":
        if not args.get("acctCd"):
            args["acctCd"] = cid
        if not args.get("company_ids"):
            args["company_ids"] = scope_context.company_ids

    return args
