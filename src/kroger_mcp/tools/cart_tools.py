"""
Cart tracking and management functionality

The Kroger Public API can only add to a cart. It cannot read the cart, remove
an item, or lower a quantity: a quantity of 0 or less is refused with
CART-2012, and adding a product already in the cart raises its quantity.
That is why this server keeps its own record of what it added (the local cart
tracking), and why the draft tools (draft_tools) let the user review items
before anything reaches Kroger.
"""
from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional, Tuple

from fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from ..errors import KrogerToolError, translate_errors
from .params import Modality, ProductId, Quantity
from .product_tools import lookup_product_summaries
from .shared import (
    get_authenticated_client,
    get_preferred_location_id,
    invalid_state_file,
    load_state_file,
    log_to_client,
    save_state_file,
    state_file_lock,
)


# Cart storage file (resolved to the shared per-user data directory)
CART_FILE = "kroger_cart.json"
ORDER_HISTORY_FILE = "kroger_order_history.json"

# Said wherever an add to the Kroger cart cannot be taken back
PERMANENT_ADD_NOTE = (
    "Adds are permanent: the Kroger API cannot remove items or lower quantities, and adding a product "
    "that is already in the cart raises its quantity. Only the user can undo an add, in the Kroger app or website."
)


class CartItem(BaseModel):
    """One item of a bulk_add_to_cart call"""

    model_config = ConfigDict(extra="forbid")

    product_id: ProductId
    quantity: Quantity = 1
    modality: Modality = "PICKUP"


def _load_cart_data() -> Dict[str, Any]:
    """Load cart data from file

    Raises:
        KrogerToolError: local_state_error, if the file cannot be read or its
            current_cart is not a list of items with integer quantities
    """
    cart_data = load_state_file(CART_FILE, {})
    cart_data.setdefault("current_cart", [])
    cart_data.setdefault("last_updated", None)
    items = cart_data["current_cart"]
    if not isinstance(items, list):
        raise invalid_state_file(CART_FILE, "current_cart is not a list")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise invalid_state_file(CART_FILE, f"current_cart[{index}] is not an object")
        quantity = item.get("quantity", 0)
        if isinstance(quantity, bool) or not isinstance(quantity, int):
            raise invalid_state_file(CART_FILE, f"current_cart[{index}].quantity is not an integer")
    return cart_data


def _save_cart_data(cart_data: Dict[str, Any]) -> None:
    """Save cart data to file"""
    save_state_file(CART_FILE, cart_data)


def _load_order_history() -> List[Dict[str, Any]]:
    """Load order history from file"""
    return load_state_file(ORDER_HISTORY_FILE, [])


def _save_order_history(history: List[Dict[str, Any]]) -> None:
    """Save order history to file"""
    save_state_file(ORDER_HISTORY_FILE, history)


def item_price(item: Dict[str, Any]) -> Optional[float]:
    """The price of one unit of a cart or draft item from its details, or None if unknown"""
    details = item.get("details") or {}
    price = details.get("sale_price") or details.get("regular_price")
    return price if isinstance(price, (int, float)) and not isinstance(price, bool) else None


def estimate_total(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Estimate the cost of cart or draft items from the prices saved in their details

    Items sold by weight are priced per unit of weight (usually per pound),
    and the quantity does not say what weight the store picks, so their
    share of the estimate is rough.
    """
    total = 0.0
    unpriced = []
    by_weight = []
    for item in items:
        price = item_price(item)
        if price is None:
            unpriced.append(item.get("product_id"))
            continue
        total += price * item.get("quantity", 0)
        if (item.get("details") or {}).get("sold_by") == "WEIGHT":
            by_weight.append(item.get("product_id"))
    estimate: Dict[str, Any] = {
        "estimated_total": round(total, 2),
        "formatted_estimated_total": f"${total:.2f}",
    }
    notes = ["Prices are the store's prices when each item was looked up, before tax, coupons, and fees."]
    if unpriced:
        estimate["unpriced_items"] = unpriced
        notes.append(f"{len(unpriced)} item(s) have no known price and are not included.")
    if by_weight:
        estimate["sold_by_weight"] = by_weight
        notes.append(
            f"{len(by_weight)} item(s) are sold by weight and priced per unit of weight, so their cost depends "
            f"on what the store weighs out."
        )
    estimate["note"] = " ".join(notes)
    return estimate


def lookup_details(product_ids: List[str]) -> Tuple[Dict[str, Dict[str, Any]], Optional[str]]:
    """Product details at the preferred location for a cart or draft record

    Never raises; see lookup_product_summaries. Without a preferred location,
    the details have no prices.
    """
    try:
        location_id = get_preferred_location_id()
    except KrogerToolError:
        location_id = None
    return lookup_product_summaries(product_ids, location_id)


def tracked_quantities(items: List[Dict[str, Any]]) -> Dict[Tuple[str, str], int]:
    """The quantity of each (product_id, modality) in the local cart tracking among items

    Returns:
        The quantity tracked for each item that the local cart tracking already has
    """
    cart = _load_cart_data()["current_cart"]
    tracked: Dict[Tuple[str, str], int] = {}
    for item in cart:
        key = (item.get("product_id"), item.get("modality"))
        tracked[key] = tracked.get(key, 0) + item.get("quantity", 0)
    keys = {(item["product_id"], item["modality"]) for item in items}
    return {key: quantity for key, quantity in tracked.items() if key in keys}


def add_items_to_local_cart(
    items: List[Dict[str, Any]], details: Optional[Dict[str, Dict[str, Any]]] = None
) -> Optional[str]:
    """Add items to the local cart tracking

    The items are already in the Kroger cart when this runs, so no failure
    here may fail the tool call: a caller that retries would add the items to
    the Kroger cart twice. That is why every exception is caught.

    Args:
        items: Cart items as sent to the API, with upc, quantity, and modality
        details: product_summary of each product by product ID, saved with its item

    Returns:
        A warning for the tool result if the local tracking was not updated, else None
    """
    details = details or {}
    try:
        with state_file_lock(CART_FILE):
            cart_data = _load_cart_data()
            current_cart = cart_data["current_cart"]
            now = datetime.now().isoformat()

            for item in items:
                product_id, quantity, modality = item["upc"], item["quantity"], item["modality"]
                # Check if item already exists in cart
                existing_item = next(
                    (i for i in current_cart if i.get("product_id") == product_id and i.get("modality") == modality),
                    None
                )
                if existing_item:
                    # Kroger adds the quantity to the item already in the cart
                    existing_item["quantity"] = existing_item.get("quantity", 0) + quantity
                    existing_item["last_updated"] = now
                    if product_id in details:
                        existing_item["details"] = details[product_id]
                else:
                    new_item = {
                        "product_id": product_id,
                        "quantity": quantity,
                        "modality": modality,
                        "added_at": now,
                        "last_updated": now
                    }
                    if product_id in details:
                        new_item["details"] = details[product_id]
                    current_cart.append(new_item)

            cart_data["last_updated"] = now
            _save_cart_data(cart_data)
    except Exception as e:
        reason = e.message if isinstance(e, KrogerToolError) else f"{type(e).__name__}: {e}"
        return (
            f"The Kroger cart was updated, but the local cart tracking was not: {reason} "
            f"Do not add the items again."
        )
    return None


def send_to_kroger_cart(cart_items: List[Dict[str, Any]]) -> None:
    """Add items to the Kroger cart in one request

    The items are added together or not at all.

    Raises:
        KrogerToolError: authentication_required, if the user is not signed in
        KrogerAPIError: If Kroger rejects the items or the request fails
    """
    client = get_authenticated_client()
    # add_to_cart returns None on success and raises a kroger-api error on failure
    client.cart.add_to_cart(cart_items)


def _add_to_cart(cart_items: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Add items to the Kroger cart, then to the local tracking with their product details

    Returns:
        Fields for the tool result: quantity_increased for items the local
        tracking already had, and the warnings of steps that did not complete

    Raises:
        KrogerToolError: authentication_required, if the user is not signed in
        KrogerAPIError: If Kroger rejects the items or the request fails
    """
    try:
        already = tracked_quantities([{"product_id": i["upc"], "modality": i["modality"]} for i in cart_items])
    except KrogerToolError:
        # Only the note below depends on it; a broken cart file is reported by the tracking update
        already = {}
    send_to_kroger_cart(cart_items)

    extra: Dict[str, Any] = {}
    # The items are in the Kroger cart now, so nothing below may raise
    details, details_warning = lookup_details([i["upc"] for i in cart_items])
    warning = add_items_to_local_cart(cart_items, details)
    if warning:
        extra["warning"] = warning
    if details_warning:
        extra["details_warning"] = details_warning
    if already:
        extra["quantity_increased"] = [
            {"product_id": product_id, "modality": modality, "previously_tracked_quantity": quantity}
            for (product_id, modality), quantity in already.items()
        ]
        extra["quantity_increased_note"] = (
            "These products were already in the cart, so Kroger added to their quantity. If that was not "
            "intended, the user must lower the quantity in the Kroger app or website."
        )
    return extra


def register_tools(mcp):
    """Register cart-related tools with the FastMCP server"""

    @mcp.tool()
    @translate_errors
    def add_items_to_cart(
        product_id: ProductId,
        quantity: Quantity = 1,
        modality: Modality = "PICKUP",
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Add a single item to the user's Kroger cart right away and track it locally.

        Adds are permanent: the Kroger API cannot remove items or lower quantities, and adding a
        product that is already in the cart raises its quantity. Only the user can undo an add, in
        the Kroger app or website. To let the user review items first, use add_to_draft and
        send_draft_to_cart instead. Use this tool only when the user asks to add an item now.

        If the user doesn't specifically indicate a preference for pickup or delivery,
        you should ask them which modality they prefer before calling this tool.

        Args:
            product_id: The product ID or UPC to add to cart
            quantity: Quantity to add (default: 1)
            modality: Fulfillment method - PICKUP or DELIVERY

        Returns:
            Dictionary confirming the item was added to cart
        """
        log_to_client(ctx, f"Adding {quantity}x {product_id} to cart with {modality} modality")

        extra = _add_to_cart([{"upc": product_id, "quantity": quantity, "modality": modality}])

        return {
            "success": True,
            "message": f"Successfully added {quantity}x {product_id} to cart",
            "product_id": product_id,
            "quantity": quantity,
            "modality": modality,
            "timestamp": datetime.now().isoformat(),
            **extra,
        }

    @mcp.tool()
    @translate_errors
    def bulk_add_to_cart(
        items: Annotated[List[CartItem], Field(min_length=1)],
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Add multiple items to the user's Kroger cart right away, in a single operation.

        Adds are permanent: the Kroger API cannot remove items or lower quantities, and adding a
        product that is already in the cart raises its quantity. Only the user can undo an add, in
        the Kroger app or website. To let the user review items first, use add_to_draft and
        send_draft_to_cart instead. Use this tool only when the user asks to add items now.

        If the user doesn't specifically indicate a preference for pickup or delivery,
        you should ask them which modality they prefer before calling this tool.

        Args:
            items: List of items to add. Each item should have:
                   - product_id: The product ID or UPC
                   - quantity: Quantity to add (default: 1)
                   - modality: PICKUP or DELIVERY (default: PICKUP)

        Returns:
            Dictionary with results for each item
        """
        log_to_client(ctx, f"Adding {len(items)} items to cart in bulk")

        extra = _add_to_cart([
            {"upc": item.product_id, "quantity": item.quantity, "modality": item.modality}
            for item in items
        ])

        return {
            "success": True,
            "message": f"Successfully added {len(items)} items to cart",
            "items_added": len(items),
            "timestamp": datetime.now().isoformat(),
            **extra,
        }

    @mcp.tool()
    @translate_errors
    def view_current_cart(ctx: Context = None) -> Dict[str, Any]:
        """
        View the current cart contents tracked locally, with product details and an estimated total.

        Note: This tool can only see items that were added via this MCP server.
        The Kroger API does not provide permission to query the actual user cart contents.
        Items added or removed in the Kroger app or website are not reflected here.

        Returns:
            Dictionary containing current cart items and summary
        """
        cart_data = _load_cart_data()
        current_cart = cart_data["current_cart"]

        # Calculate summary
        total_quantity = sum(item.get("quantity", 0) for item in current_cart)
        pickup_items = [item for item in current_cart if item.get("modality") == "PICKUP"]
        delivery_items = [item for item in current_cart if item.get("modality") == "DELIVERY"]

        return {
            "success": True,
            "current_cart": current_cart,
            "summary": {
                "total_items": len(current_cart),
                "total_quantity": total_quantity,
                "pickup_items": len(pickup_items),
                "delivery_items": len(delivery_items),
                "last_updated": cart_data.get("last_updated"),
                **(estimate_total(current_cart) if current_cart else {}),
            },
            "note": (
                "This is the server's record of what it added. The real Kroger cart can differ if the user "
                "changed it in the Kroger app or website."
            ),
        }

    @mcp.tool()
    @translate_errors
    def remove_from_cart(
        product_id: Annotated[str, Field(min_length=1, description="The product ID to remove from the local tracking")],
        modality: Optional[Modality] = None,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Remove an item from the local cart tracking only.

        IMPORTANT: This tool CANNOT remove items from the actual Kroger cart in the app/website.
        It only updates our local tracking to stay in sync. The user must remove the item from
        their actual cart through the Kroger app or website themselves.

        Use this tool only when:
        1. The user has already removed an item from their Kroger cart through the app/website
        2. You need to update the local tracking to reflect that change

        Args:
            product_id: The product ID to remove
            modality: Specific modality to remove (if None, removes all instances)

        Returns:
            Dictionary confirming the removal from local tracking
        """
        with state_file_lock(CART_FILE):
            cart_data = _load_cart_data()
            current_cart = cart_data["current_cart"]
            original_count = len(current_cart)

            if modality:
                # Remove specific modality
                cart_data["current_cart"] = [
                    item for item in current_cart
                    if not (item.get("product_id") == product_id and item.get("modality") == modality)
                ]
            else:
                # Remove all instances
                cart_data["current_cart"] = [
                    item for item in current_cart
                    if item.get("product_id") != product_id
                ]

            items_removed = original_count - len(cart_data["current_cart"])

            if items_removed > 0:
                cart_data["last_updated"] = datetime.now().isoformat()
                _save_cart_data(cart_data)
                message = f"Removed {items_removed} items from local cart tracking"
            else:
                message = (
                    f"No item with product_id {product_id}"
                    + (f" and modality {modality}" if modality else "")
                    + " is in the local cart tracking, so nothing was removed. See view_current_cart."
                )

        return {
            "success": True,
            "message": message,
            "items_removed": items_removed,
            "product_id": product_id,
            "modality": modality
        }

    @mcp.tool()
    @translate_errors
    def clear_current_cart(ctx: Context = None) -> Dict[str, Any]:
        """
        Clear all items from the local cart tracking only.

        IMPORTANT: This tool CANNOT remove items from the actual Kroger cart in the app/website.
        It only clears our local tracking. The user must remove items from their actual cart
        through the Kroger app or website themselves.

        Use this tool only when:
        1. The user has already cleared their Kroger cart through the app/website
        2. You need to update the local tracking to reflect that change
        3. Or when the local tracking is out of sync with the actual cart

        Returns:
            Dictionary confirming the local cart tracking was cleared
        """
        with state_file_lock(CART_FILE):
            cart_data = _load_cart_data()
            items_count = len(cart_data["current_cart"])

            cart_data["current_cart"] = []
            cart_data["last_updated"] = datetime.now().isoformat()
            _save_cart_data(cart_data)

        return {
            "success": True,
            "message": f"Cleared {items_count} items from local cart tracking",
            "items_cleared": items_count
        }

    @mcp.tool()
    @translate_errors
    def mark_order_placed(
        order_notes: Optional[str] = None,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Mark the current cart as an order that has been placed and move it to order history.
        Use this after you've completed checkout on the Kroger website/app.

        Args:
            order_notes: Optional notes about the order

        Returns:
            Dictionary confirming the order was recorded
        """
        with state_file_lock(CART_FILE), state_file_lock(ORDER_HISTORY_FILE):
            cart_data = _load_cart_data()
            current_cart = cart_data["current_cart"]

            if not current_cart:
                raise KrogerToolError(
                    "empty_cart",
                    "The local cart tracking is empty, so there is no order to record.",
                    next_step=(
                        "Only items added with add_items_to_cart, bulk_add_to_cart, or send_draft_to_cart are "
                        "tracked. Items added in the Kroger app or website are not."
                    ),
                )

            # Create order record
            order_record = {
                "items": current_cart.copy(),
                "placed_at": datetime.now().isoformat(),
                "item_count": len(current_cart),
                "total_quantity": sum(item.get("quantity", 0) for item in current_cart),
                "estimated_total": estimate_total(current_cart)["estimated_total"],
                "notes": order_notes
            }

            # Load and update order history
            order_history = _load_order_history()
            order_history.append(order_record)
            _save_order_history(order_history)

            # Clear current cart
            cart_data["current_cart"] = []
            cart_data["last_updated"] = datetime.now().isoformat()
            try:
                _save_cart_data(cart_data)
            except KrogerToolError as e:
                raise KrogerToolError(
                    "local_state_error",
                    f"The order was added to the order history, but the local cart could not be cleared: {e.message}",
                    next_step="Do not call mark_order_placed again. Fix the problem, then call clear_current_cart.",
                    details=e.details,
                ) from e

        return {
            "success": True,
            "message": f"Marked order with {order_record['item_count']} items as placed",
            "order_id": len(order_history),  # Simple order ID based on history length
            "items_placed": order_record["item_count"],
            "total_quantity": order_record["total_quantity"],
            "estimated_total": order_record["estimated_total"],
            "placed_at": order_record["placed_at"]
        }

    @mcp.tool()
    @translate_errors
    def view_order_history(
        limit: Annotated[int, Field(ge=1, le=50, description="Number of recent orders to show (1-50)")] = 10,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        View the history of placed orders.

        Note: This tool can only see orders that were explicitly marked as placed via this MCP server.
        The Kroger API does not provide permission to query the actual order history from Kroger's systems.

        Args:
            limit: Number of recent orders to show (1-50)

        Returns:
            Dictionary containing order history
        """
        order_history = _load_order_history()

        # Sort by placed_at date (most recent first) and limit
        sorted_orders = sorted(order_history, key=lambda x: x.get("placed_at", ""), reverse=True)
        limited_orders = sorted_orders[:limit]

        # Calculate summary stats
        total_orders = len(order_history)
        total_items_all_time = sum(order.get("item_count", 0) for order in order_history)
        total_quantity_all_time = sum(order.get("total_quantity", 0) for order in order_history)

        return {
            "success": True,
            "orders": limited_orders,
            "showing": len(limited_orders),
            "summary": {
                "total_orders": total_orders,
                "total_items_all_time": total_items_all_time,
                "total_quantity_all_time": total_quantity_all_time
            }
        }
