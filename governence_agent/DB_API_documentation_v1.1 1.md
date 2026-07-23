# DB API documentation


## Authentication Endpoint

The DB API uses JWT (JSON Web Token) based authentication. To access protected resources, you need to obtain a token by authenticating with valid credentials.

### Sign-In Endpoint

**URL:** `POST https://db-api.frontierdental.com/authentication/sign-in`

**Body Content-Type:** `application/json`

### Request Format

```json
{
  "username": "string",
  "password": "string"
}
```

Both fields are required:
- `username`: Your DB API system username
- `password`: Your DB API system password

### Response Format

**Success Response (200 OK):**

```json
{
  "access_token": "eyJhbGciOiJIUzI1NiIsInR5c..."
}
```

### Using the Authentication Token

After obtaining a token, include it in the header of all subsequent API requests:

```
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5c...
```

### Token Expiry

Each JWT token will expire 24 hours after being created.

# Offset pagination graphql query

**URL**: POST https://db-api.frontierdental.com/graphql

**Required header**: Authorization: Bearer [insert-your-jwt-token-here]

**Body Content-Type**: application/json

## Overview

The `findWithOffsetPagination` query allows you to retrieve data from any table in the DB API system using traditional page-based pagination.


## Query Structure

```graphql
query {
  findWithOffsetPagination(
    table: "EntityName",
    options: {
      select: { /* fields to include */ },
      where: { /* filter conditions */ },
      orderBy: { /* sorting criteria */ },
      page: 1,
      pageSize: 20
    }
  ) {
    items
    page
    pageSize
    hasMore
  }
}
```

## Parameters

### Required Parameters

- `table` (string): The name of the entity table to query (e.g., "Account", "Branch", "")

### Optional Parameters (in `options` object)

- `select` (object): Specifies which fields to include in the results
- `where` (object): Filtering conditions to apply
- `orderBy` (object or array): Sorting criteria
- `page` (number): The page number to retrieve (default: 1, min: 1)
- `pageSize` (number): Number of items per page (default: 10, max: 1000)

## Response Structure

```json
{
  "data": {
    "findWithOffsetPagination": {
      "items": [...],      // Array of entity objects
      "page": 1,           // Current page number
      "pageSize": 20,      // Items per page
      "hasMore": true,     // Whether more pages exist
    }
  }
}
```

## Parameter Details

### Select Parameter

The `select` parameter lets you specify which fields to include in the response. It uses a key-value object where:
- Keys are field names
- Values are boolean (`true` to include the field)

```graphql
select: {
  accountId: true,
  accountCd: true,
  description: true,
  type: true
}
```

If omitted, all accessible fields will be returned.

### Where Parameter

The `where` parameter allows you to filter the results using a variety of operators:

#### Comparison Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `not` | Not equal to | `{ "branchCd": { "not": "Toronto" } }` |
| `in` | In a list of values | `{ "branchCd": { "in": ["Toronto", "Montreal"] } }` |
| `notIn` | Not in a list of values | `{ "branchCd": { "notIn": ["Toronto", "Montreal"] } }` |
| `lt` | Less than | `{ "accountId": { "lt": 3 } }` |
| `lte` | Less than or equal to | `{ "accountId": { "lte": 5000 } }` |
| `gt` | Greater than | `{ "accountId": { "gt": 50 } }` |
| `gte` | Greater than or equal to | `{ "accountId": { "gte": 100 } }` |

#### String Operators

| Operator | Description | Example |
|----------|-------------|---------|
| `contains` | Contains substring | `{ "branchCd": { "contains": "Revenue" } }` |
| `startsWith` | Starts with substring | `{ "branchCd": { "startsWith": "Rev" } }` |
| `endsWith` | Ends with substring | `{ "branchCd": { "endsWith": "evenue" } }` |


### OrderBy Parameter

The `orderBy` parameter controls how results are sorted:

```graphql
// Field sorting
orderBy: { accountCd: "ASC" }
```

Sorting directions:
- `ASC`: Ascending order (A-Z, 0-9)
- `DESC`: Descending order (Z-A, 9-0)

## Examples

### Basic Query

```graphql
query {
  findWithOffsetPagination(
    table: "Account",
    options: {
      page: 1,
      pageSize: 10
    }
  ) {
    items,
    page,
    pageSize,
    hasMore
  }
}
```

### Query with Selected Fields

```graphql
query {
  findWithOffsetPagination(
    table: "Account",
    options: {
      select: {
        accountId: true,
        accountCd: true,
        description: true,
        type: true
      },
      page: 1,
      pageSize: 10
    }
  ) {
    items
    page
    pageSize
    hasMore
  }
}
```


### Query with selected fields, a where clause and an orderBy clause

```
query {
  findWithOffsetPagination(
    table: "Account",
    options: {
      select: {
        companyId: true,
        accountId: true,
        accountCd: true,
        accountingType: true,
        type: true,
        controlAccountModule: true,
        allowManualEntry: true,
        coaOrder: true,
        accountClassId: true,
        accountGroupId: true,
        active: true,
        description: true,
        postOption: true,
        directPost: true,
        curyId: true,
        branchId: true,
        isCashAccount: true,
        createdDateTime: true,
        lastModifiedDateTime: true
      },
      where: {
        companyId: 2,
        accountingType: { not: "A" },
        coaOrder: { lte: 100 },
        accountId: { gt: 3000 },
        type: { in: ["A", "L", "E"] },
        postOption: { notIn: ["P", "S"] },
        description: { contains: "cash" }
      },
      orderBy: {
        accountId: "DESC"
      },
      page: 1,
      pageSize: 20
    }
  ) {
    items,
    page,
    pageSize,
    hasMore
  }
}
```

At the end of query the user may select four parameters to include. These parameters are items, page, pageSize and hasMore.
Items contains the list of all the results, page is the current page number, pageSize is the size of each page and hasMore
indicates if there are more results to be queried.

# Cursor pagination graphql query


## Endpoint

**URL**: POST https://db-api.frontierdental.com/graphql

**Required header**: Authorization: Bearer [insert-your-jwt-token-here]

**Body Content-Type**: application/json

## Query Structure

```graphql
query {
  findWithCursorPagination(
    table: "EntityName",
    options: {
      select: { /* fields to include */ },
      orderBy: { /* sorting criteria */ },
      pageSize: number,
      cursor: "base64EncodedCursor"
    }
  ) {
    items
    pageSize
    hasMore
    cursor
  }
}
```

## Parameters

### Required Parameters

- `table` (string): The name of the entity table to query (e.g., "Account", "Customer", "GLTran")
- `options.orderBy` (object): Sorting criteria - required for cursor pagination to function properly

### Optional Parameters

- `select` (object): Specifies which fields to include in the results
- `pageSize` (number): Number of items per page (default: 10, max: 1000)
- `cursor` (string): A base64-encoded cursor pointing to the position after the last item of the previous page. Omit this parameter for the first page.

## Response Structure

```json
{
  "data": {
    "findWithCursorPagination": {
      "items": [...],                     // Array of entity objects
      "pageSize": 20,                     // Items per page
      "hasMore": true,                    // Whether more pages exist
      "nextCursor": "base64EncodedString" // Cursor for retrieving the next page
    }
  }
}
```

## Parameter Details

### Select Parameter

The `select` parameter lets you specify which fields to include in the response. It uses a key-value object where:
- Keys are field names
- Values are boolean (`true` to include the field)

```graphql
select: {
  accountId: true,
  accountCd: true,
  description: true,
  type: true
}
```

If omitted, all accessible fields will be returned.

### OrderBy Parameter

The `orderBy` parameter controls how results are sorted. For cursor pagination, this parameter is **required** and crucial for the cursor mechanism to work properly.

```graphql
// Single field sorting
orderBy: { accountCd: "ASC" }

// Multi-field sorting
orderBy: [
  { type: "ASC" },
  { accountCd: "ASC" }
]
```

Sorting directions:
- `ASC`: Ascending order (A-Z, 0-9)
- `DESC`: Descending order (Z-A, 9-0)

**Important**: The cursor is encoded based on the orderBy field values. For consistent pagination, always use the same orderBy criteria for subsequent requests.

### Cursor Parameter

The `cursor` parameter is a base64-encoded string that points to the position after the last item of the previous page. It is returned as `nextCursor` in the response and should be used for fetching the next page.

- Omit this parameter when fetching the first page
- Include it in subsequent requests to fetch following pages
- The cursor is based on the values of the field(s) specified in `orderBy`

## Examples

### First Page Request (No Cursor)

```graphql
query {
  findWithCursorPagination(
    table: "GLTran",
    options: {
      select: {
        companyId: true,
        module: true,
        batchNbr: true,
        lineNbr: true
    }
    orderBy: {
      companyId: "ASC"
      module: "ASC",
      batchNbr: "ASC",
      lineNbr: "ASC"
    },
    pageSize: 1000,
  }
  ) {
    items
    pageSize
    hasMore
    nextCursor
  }
}
```

### Subsequent Page Request (With Cursor)

```graphql

query {
  findWithCursorPagination(
    table: "GLTran",
    options: {
      select: {
        companyId: true,
        module: true,
        batchNbr: true,
        lineNbr: true
    }
    orderBy: {
      companyId: "ASC"
      module: "ASC",
      batchNbr: "ASC",
      lineNbr: "ASC"
    },
    pageSize: 1000,
    cursor: "eyJjb21wYW55SWQiOjIsIm1vZHVsZSI6IkFQIiwiYmF0Y2hOYnIiOiIwMDAwMTM3ODEiLCJsaW5lTmJyIjozfQ=="
  }
  ) {
    items
    pageSize
    hasMore
    nextCursor
  }
}

```
