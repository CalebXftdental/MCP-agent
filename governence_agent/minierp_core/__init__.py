"""minierp_core -- the shared miniERP data-access layer for all backends.

One source of the GraphQL transport (client) so the per-domain backends no
longer each carry their own byte-identical copy. Domain query logic
(entity/field maps, tool builders) stays per-domain; only the transport +
custom resolvers live here.

Public surface:
    from minierp_core import (
        find_with_offset_pagination, find_with_cursor_pagination,
        get_sales_order_data_by_order_number,
        GraphQLConfigError, GraphQLAuthError, GraphQLQueryError,
    )
"""
from __future__ import annotations

from .graphql_client import (
    GraphQLAuthError,
    GraphQLConfigError,
    GraphQLQueryError,
    fetch_page_or_all,
    find_with_cursor_pagination,
    find_with_offset_pagination,
    get_order_address_data_by_order_number,
    get_sales_order_data_by_order_number,
    paginate_all,
)

__all__ = [
    "find_with_offset_pagination",
    "find_with_cursor_pagination",
    "paginate_all",
    "fetch_page_or_all",
    "get_sales_order_data_by_order_number",
    "get_order_address_data_by_order_number",
    "GraphQLConfigError",
    "GraphQLAuthError",
    "GraphQLQueryError",
]
