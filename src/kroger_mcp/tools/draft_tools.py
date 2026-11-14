"""
Draft cart: items the user reviews before they are sent to the Kroger cart

The Kroger Public API can only add to a cart, and an add cannot be taken
back: removing an item or lowering a quantity can only be done by the user in
the Kroger app or website. So items are first collected in a local draft,
where they can be changed freely, and send_draft_to_cart adds the whole draft
to the Kroger cart in one request once the user approves it.
"""
from datetime import datetime
from typing import Annotated, Any, Dict, List, Optional

from fastmcp import Context
from pydantic import BaseModel, ConfigDict, Field

from kroger_api.errors import KrogerAPIError

from ..errors import KrogerToolError, translate_errors
from .cart_tools import (
    PERMANENT_ADD_NOTE,
    add_items_to_local_cart,
    estimate_total,
    lookup_details,
    send_to_kroger_cart,
    tracked_quantities,
)
from .params import Modality, ProductId, Quantity
from .shared import (
    invalid_state_file,
    load_state_file,
    log_to_client,
    save_state_file,
    state_file_lock,
)

DRAFT_FILE = "kroger_draft.json"


class DraftItem(BaseModel):
    """One item of an add_to_draft call"""

    model_config = ConfigDict(extra="forbid")

    product_id: ProductId
    quantity: Quantity = 1
    modality: Modality = "PICKUP"
    note: Optional[str] = Field(
        default=None,
        description="Why the item is on the list, for example 'for the apple pie' or the user's own words",
    )


def _load_draft() -> Dict[str, Any]:
    """Load the draft

    Raises:
        KrogerToolError: local_state_error, if the file cannot be read or its
            items are not a list of items with a product_id, modality, and
            positive integer quantity
    """
    draft = load_state_file(DRAFT_FILE, {})
    draft.setdefault("items", [])
    draft.setdefault("last_updated", None)
    items = draft["items"]
    if not isinstance(items, list):
        raise invalid_state_file(DRAFT_FILE, "items is not a list")
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            raise invalid_state_file(DRAFT_FILE, f"items[{index}] is not an object")
        if not isinstance(item.get("product_id"), str) or item.get("modality") not in ("PICKUP", "DELIVERY"):
            raise invalid_state_file(DRAFT_FILE, f"items[{index}] has no valid product_id and modality")
        quantity = item.get("quantity")
        if isinstance(quantity, bool) or not isinstance(quantity, int) or quantity < 1:
            raise invalid_state_file(DRAFT_FILE, f"items[{index}].quantity is not an integer of 1 or more")
    return draft


def _save_draft(draft: Dict[str, Any]) -> None:
    draft["last_updated"] = datetime.now().isoformat()
    save_state_file(DRAFT_FILE, draft)


def _find(items: List[Dict[str, Any]], product_id: str, modality: Optional[str]) -> List[Dict[str, Any]]:
    return [
        item for item in items
        if item["product_id"] == product_id and (modality is None or item["modality"] == modality)
    ]


def _not_in_draft(product_id: str, modality: Optional[str]) -> KrogerToolError:
    return KrogerToolError(
        "not_in_draft",
        f"The draft has no item with product_id {product_id}" + (f" and modality {modality}" if modality else "") + ".",
        next_step="Call view_draft to see the product IDs and modalities in the draft.",
        details={"product_id": product_id, "modality": modality},
    )


def _draft_view(draft: Dict[str, Any]) -> Dict[str, Any]:
    """The draft's items, estimated total, and the items already in the Kroger cart"""
    items = draft["items"]
    view: Dict[str, Any] = {
        "items": items,
        "summary": {
            "total_items": len(items),
            "total_quantity": sum(item["quantity"] for item in items),
            "last_updated": draft.get("last_updated"),
            **(estimate_total(items) if items else {}),
        },
    }
    if items:
        try:
            already = tracked_quantities(items)
        except KrogerToolError:
            already = {}
        if already:
            view["already_in_cart"] = [
                {"product_id": product_id, "modality": modality, "tracked_quantity": quantity}
                for (product_id, modality), quantity in already.items()
            ]
            view["already_in_cart_note"] = (
                "The local cart tracking says these products were already sent to the Kroger cart. Sending "
                "the draft adds its quantity to theirs. Ask the user whether they want more, or remove them "
                "from the draft."
            )
    return view


def register_tools(mcp):
    """Register the draft cart tools with the FastMCP server"""

    @mcp.tool()
    @translate_errors
    def add_to_draft(
        items: Annotated[List[DraftItem], Field(min_length=1, max_length=50)],
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Add items to the draft cart, a local list the user reviews before anything is sent to Kroger.

        Prefer this to add_items_to_cart and bulk_add_to_cart: an add to the Kroger cart cannot be
        taken back, but the draft can be changed freely with update_draft_item and remove_from_draft.
        Adding a product that is already in the draft with the same modality raises its quantity.
        The product's name, size, and price at the preferred store are looked up and saved with it.

        Show the user the draft (view_draft) and get their approval before send_draft_to_cart.
        If the user has not said whether they want pickup or delivery, ask before sending.

        Args:
            items: The items to add (1-50). Each has a product_id, an optional quantity (default 1),
                an optional modality (PICKUP or DELIVERY, default PICKUP), and an optional note.

        Returns:
            The draft after the change, with an estimated total
        """
        log_to_client(ctx, f"Adding {len(items)} items to the draft")
        # The lookup is slow, so it runs before the draft is locked
        details, details_warning = lookup_details([item.product_id for item in items])

        with state_file_lock(DRAFT_FILE):
            draft = _load_draft()
            now = datetime.now().isoformat()
            for new in items:
                existing = _find(draft["items"], new.product_id, new.modality)
                if existing:
                    item = existing[0]
                    item["quantity"] += new.quantity
                    item["last_updated"] = now
                    if new.note:
                        item["note"] = f"{item['note']}; {new.note}" if item.get("note") else new.note
                else:
                    item = {
                        "product_id": new.product_id,
                        "quantity": new.quantity,
                        "modality": new.modality,
                        "note": new.note,
                        "added_at": now,
                        "last_updated": now,
                    }
                    draft["items"].append(item)
                if new.product_id in details:
                    item["details"] = details[new.product_id]
            _save_draft(draft)

        result = {"success": True, "message": f"Added {len(items)} items to the draft", **_draft_view(draft)}
        if details_warning:
            result["details_warning"] = details_warning
        return result

    @mcp.tool()
    @translate_errors
    def view_draft(ctx: Context = None) -> Dict[str, Any]:
        """
        View the draft cart: the items waiting for the user's approval, with product details
        and an estimated total. Nothing in the draft is in the Kroger cart yet.

        already_in_cart lists draft items that this server has already sent to the Kroger cart,
        where sending them again would raise their quantity.

        Returns:
            The draft's items and summary
        """
        return {"success": True, **_draft_view(_load_draft())}

    @mcp.tool()
    @translate_errors
    def update_draft_item(
        product_id: ProductId,
        quantity: Annotated[int, Field(ge=0, description="The new quantity; 0 removes the item from the draft")],
        modality: Annotated[Optional[Modality], Field(
            description="The modality of the item to change. Needed only if the draft has the product with both modalities.",
        )] = None,
        new_modality: Annotated[Optional[Modality], Field(description="Change the item's modality to this")] = None,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Set the quantity, or change the modality, of an item in the draft cart.
        This changes only the draft; nothing is sent to Kroger.

        Args:
            product_id: The product ID of the draft item
            quantity: The new quantity; 0 removes the item
            modality: Which item to change, if the product is in the draft with both modalities
            new_modality: Change the item to PICKUP or DELIVERY

        Returns:
            The draft after the change
        """
        with state_file_lock(DRAFT_FILE):
            draft = _load_draft()
            matches = _find(draft["items"], product_id, modality)
            if not matches:
                raise _not_in_draft(product_id, modality)
            if len(matches) > 1:
                raise KrogerToolError(
                    "invalid_parameter",
                    f"The draft has product {product_id} for both PICKUP and DELIVERY.",
                    next_step="Call update_draft_item again with modality set to the item to change.",
                    details={"param": "modality", "product_id": product_id},
                )
            item = matches[0]
            if quantity == 0:
                draft["items"].remove(item)
                message = f"Removed {product_id} ({item['modality']}) from the draft"
            else:
                item["quantity"] = quantity
                item["last_updated"] = datetime.now().isoformat()
                message = f"Set {product_id} to quantity {quantity}"
                if new_modality and new_modality != item["modality"]:
                    other = _find(draft["items"], product_id, new_modality)
                    if other:
                        # Merge into the item that already has the new modality
                        other[0]["quantity"] += quantity
                        draft["items"].remove(item)
                        message = f"Moved {product_id} to the {new_modality} item, now quantity {other[0]['quantity']}"
                    else:
                        item["modality"] = new_modality
                        message += f" for {new_modality}"
            _save_draft(draft)

        return {"success": True, "message": message, **_draft_view(draft)}

    @mcp.tool()
    @translate_errors
    def remove_from_draft(
        product_id: ProductId,
        modality: Annotated[Optional[Modality], Field(
            description="Remove only the item with this modality; if not given, remove the product for both",
        )] = None,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Remove an item from the draft cart. This changes only the draft; nothing is sent to Kroger.

        Args:
            product_id: The product ID of the draft item
            modality: Remove only the PICKUP or DELIVERY item (default: both)

        Returns:
            The draft after the change
        """
        with state_file_lock(DRAFT_FILE):
            draft = _load_draft()
            matches = _find(draft["items"], product_id, modality)
            if not matches:
                raise _not_in_draft(product_id, modality)
            draft["items"] = [item for item in draft["items"] if item not in matches]
            _save_draft(draft)

        return {
            "success": True,
            "message": f"Removed {len(matches)} item(s) with product_id {product_id} from the draft",
            **_draft_view(draft),
        }

    @mcp.tool()
    @translate_errors
    def clear_draft(ctx: Context = None) -> Dict[str, Any]:
        """
        Remove every item from the draft cart. Nothing is sent to Kroger, and the Kroger cart
        and the local cart tracking are not changed.

        Returns:
            How many items were removed
        """
        with state_file_lock(DRAFT_FILE):
            draft = _load_draft()
            count = len(draft["items"])
            draft["items"] = []
            _save_draft(draft)
        return {"success": True, "message": f"Removed {count} items from the draft", "items_cleared": count}

    @mcp.tool()
    @translate_errors
    def send_draft_to_cart(
        allow_quantity_increase: Annotated[bool, Field(description=(
            "Send items that this server already sent to the Kroger cart, raising their quantity there. "
            "Set it only after the user confirms they want more of those items."
        ))] = False,
        ctx: Context = None
    ) -> Dict[str, Any]:
        """
        Send every item in the draft cart to the user's Kroger cart in one request, then empty the
        draft and record the items in the local cart tracking.

        This cannot be taken back: the Kroger API cannot remove items or lower quantities. Only call
        it after showing the user the draft (view_draft) and getting their approval in this
        conversation.

        If any draft item was already sent to the Kroger cart, the call fails with
        quantity_would_increase unless allow_quantity_increase is true, because Kroger would add
        the draft's quantity to the quantity already in the cart.

        Args:
            allow_quantity_increase: Send items already in the cart anyway (default false)

        Returns:
            What was sent, and the estimated total
        """
        with state_file_lock(DRAFT_FILE):
            draft = _load_draft()
            items = draft["items"]
            if not items:
                raise KrogerToolError(
                    "empty_draft",
                    "The draft is empty, so there is nothing to send.",
                    next_step="Add items with add_to_draft first.",
                )

            already = tracked_quantities(items)
            if already and not allow_quantity_increase:
                raise KrogerToolError(
                    "quantity_would_increase",
                    "Some draft items are already in the Kroger cart, according to the local cart tracking, "
                    "and sending them would add to their quantity there. Nothing was sent.",
                    next_step=(
                        "Ask the user whether they want more of these items. If they do, call send_draft_to_cart "
                        "with allow_quantity_increase true. If not, remove them with remove_from_draft and send "
                        "again. If the user removed them in the Kroger app, call remove_from_cart first."
                    ),
                    details={"items": [
                        {"product_id": product_id, "modality": modality, "tracked_quantity": quantity}
                        for (product_id, modality), quantity in already.items()
                    ]},
                )

            cart_items = [
                {"upc": item["product_id"], "quantity": item["quantity"], "modality": item["modality"]}
                for item in items
            ]
            log_to_client(ctx, f"Sending {len(cart_items)} draft items to the Kroger cart")
            try:
                send_to_kroger_cart(cart_items)
            except KrogerAPIError as e:
                error = KrogerToolError.from_api_error(e)
                if e.outcome_unknown:
                    error.next_step = (
                        "Do not call send_draft_to_cart again yet: Kroger may have added the items anyway. Ask "
                        "the user to check their cart in the Kroger app or website. If the items are there, "
                        "call clear_draft. If they are not, call send_draft_to_cart again. The draft was kept."
                    )
                else:
                    error.message += "\nThe draft was kept, and nothing was added to the Kroger cart."
                raise error from e

            # The items are in the Kroger cart now, so nothing below may raise:
            # a caller that retried would add them twice
            details = {item["product_id"]: item["details"] for item in items if item.get("details")}
            warnings = []
            tracking_warning = add_items_to_local_cart(cart_items, details)
            if tracking_warning:
                warnings.append(tracking_warning)
            sent = list(items)
            try:
                draft["items"] = []
                _save_draft(draft)
            except Exception as e:
                reason = e.message if isinstance(e, KrogerToolError) else f"{type(e).__name__}: {e}"
                warnings.append(
                    f"The items were sent, but the draft could not be emptied: {reason} Do not call "
                    f"send_draft_to_cart again; call clear_draft once the problem is fixed."
                )

        result = {
            "success": True,
            "message": f"Sent {len(sent)} items to the Kroger cart",
            "items_sent": sent,
            "summary": estimate_total(sent),
            "note": PERMANENT_ADD_NOTE,
            "timestamp": datetime.now().isoformat(),
        }
        if warnings:
            result["warning"] = " ".join(warnings)
        return result
