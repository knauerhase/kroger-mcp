"""The draft cart, product details in the cart records, and estimated totals"""
import json

import pytest
import requests

from kroger_mcp.tools import cart_tools, draft_tools, product_tools

from conftest import BASE, call_error, call_ok, kroger_error, save_user_token

MILK = "0001111041700"
BANANAS = "0000000004011"
EGGS = "0001111060903"
CART_URL = f"{BASE}/v1/cart/add"

DETAILS = {
    MILK: {"description": "2% Milk", "brand": "Kroger", "size": "1 gal", "sold_by": "UNIT",
           "regular_price": 3.49, "sale_price": 2.99, "product_url": None},
    BANANAS: {"description": "Bananas", "brand": None, "size": "1 lb", "sold_by": "WEIGHT",
              "regular_price": 0.59, "sale_price": None, "product_url": None},
}


@pytest.fixture
def lookups(monkeypatch):
    """Product details are found for MILK and BANANAS; the product IDs of each lookup are recorded"""
    calls = []

    def lookup(product_ids, location_id):
        calls.append(list(product_ids))
        found = {i: DETAILS[i] for i in product_ids if i in DETAILS}
        missing = [i for i in product_ids if i not in DETAILS]
        return found, (f"Kroger has no product details for {', '.join(missing)}." if missing else None)

    monkeypatch.setattr(cart_tools, "lookup_product_summaries", lookup)
    return calls


def cart_add(requests_mock, status_code=204):
    return requests_mock.put(CART_URL, status_code=status_code)


# --- building the draft -------------------------------------------------------------


async def test_add_to_draft_saves_details_and_merges_quantities(mcp, lookups):
    result = await call_ok(mcp, "add_to_draft", {"items": [
        {"product_id": MILK, "quantity": 2, "note": "for cereal"},
        {"product_id": BANANAS},
    ]})
    assert lookups == [[MILK, BANANAS]]
    milk = result["items"][0]
    assert (milk["quantity"], milk["modality"], milk["note"]) == (2, "PICKUP", "for cereal")
    assert milk["details"]["description"] == "2% Milk"

    result = await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK, "note": "for pie"}]})
    milk = result["items"][0]
    assert milk["quantity"] == 3
    assert milk["note"] == "for cereal; for pie"
    assert len(result["items"]) == 2


async def test_same_product_with_another_modality_is_a_separate_item(mcp, lookups):
    await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK}]})
    result = await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK, "modality": "DELIVERY"}]})
    assert [(i["modality"], i["quantity"]) for i in result["items"]] == [("PICKUP", 1), ("DELIVERY", 1)]


async def test_view_draft_estimates_the_total(mcp, lookups):
    await call_ok(mcp, "add_to_draft", {"items": [
        {"product_id": MILK, "quantity": 2},
        {"product_id": BANANAS, "quantity": 3},
        {"product_id": EGGS},
    ]})
    summary = (await call_ok(mcp, "view_draft"))["summary"]
    # Milk at its sale price, bananas per pound, eggs without a price
    assert summary["estimated_total"] == pytest.approx(2 * 2.99 + 3 * 0.59)
    assert summary["unpriced_items"] == [EGGS]
    assert summary["sold_by_weight"] == [BANANAS]
    assert summary["total_quantity"] == 6


async def test_lookup_failure_still_adds_to_the_draft(mcp, lookups):
    result = await call_ok(mcp, "add_to_draft", {"items": [{"product_id": EGGS}]})
    assert result["items"][0]["product_id"] == EGGS
    assert "details" not in result["items"][0]
    assert EGGS in result["details_warning"]


async def test_empty_draft(mcp):
    result = await call_ok(mcp, "view_draft")
    assert result["items"] == []
    assert result["summary"]["total_items"] == 0


# --- changing the draft -------------------------------------------------------------


async def test_update_draft_item(mcp, lookups):
    await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK, "quantity": 3}]})
    result = await call_ok(mcp, "update_draft_item", {"product_id": MILK, "quantity": 1, "new_modality": "DELIVERY"})
    assert [(i["quantity"], i["modality"]) for i in result["items"]] == [(1, "DELIVERY")]

    result = await call_ok(mcp, "update_draft_item", {"product_id": MILK, "quantity": 0})
    assert result["items"] == []


async def test_update_merges_into_the_item_with_the_new_modality(mcp, lookups):
    await call_ok(mcp, "add_to_draft", {"items": [
        {"product_id": MILK, "quantity": 2}, {"product_id": MILK, "quantity": 1, "modality": "DELIVERY"},
    ]})
    result = await call_ok(mcp, "update_draft_item",
                           {"product_id": MILK, "modality": "PICKUP", "quantity": 2, "new_modality": "DELIVERY"})
    assert [(i["quantity"], i["modality"]) for i in result["items"]] == [(3, "DELIVERY")]


async def test_update_needs_the_modality_when_the_product_has_both(mcp, lookups):
    await call_ok(mcp, "add_to_draft", {"items": [
        {"product_id": MILK}, {"product_id": MILK, "modality": "DELIVERY"},
    ]})
    error, _ = await call_error(mcp, "update_draft_item", {"product_id": MILK, "quantity": 5})
    assert error["type"] == "invalid_parameter"
    assert error["param"] == "modality"


async def test_changing_an_item_not_in_the_draft(mcp):
    for tool, arguments in [("update_draft_item", {"product_id": MILK, "quantity": 2}),
                            ("remove_from_draft", {"product_id": MILK})]:
        error, _ = await call_error(mcp, tool, arguments)
        assert error["type"] == "not_in_draft"
        assert "view_draft" in error["next_step"]


async def test_remove_from_draft_and_clear_draft(mcp, lookups):
    await call_ok(mcp, "add_to_draft", {"items": [
        {"product_id": MILK}, {"product_id": MILK, "modality": "DELIVERY"}, {"product_id": BANANAS},
    ]})
    result = await call_ok(mcp, "remove_from_draft", {"product_id": MILK, "modality": "DELIVERY"})
    assert [(i["product_id"], i["modality"]) for i in result["items"]] == [(MILK, "PICKUP"), (BANANAS, "PICKUP")]
    result = await call_ok(mcp, "remove_from_draft", {"product_id": MILK})
    assert [i["product_id"] for i in result["items"]] == [BANANAS]
    assert (await call_ok(mcp, "clear_draft"))["items_cleared"] == 1
    assert (await call_ok(mcp, "view_draft"))["items"] == []


async def test_invalid_draft_file(mcp, isolated):
    (isolated / draft_tools.DRAFT_FILE).write_text(json.dumps({"items": [{"product_id": MILK, "quantity": 0}]}))
    error, _ = await call_error(mcp, "view_draft")
    assert error["type"] == "local_state_error"


# --- sending the draft --------------------------------------------------------------


async def test_send_draft_sends_one_request_and_moves_the_items_to_the_cart(mcp, requests_mock, lookups):
    save_user_token()
    add = cart_add(requests_mock)
    await call_ok(mcp, "add_to_draft", {"items": [
        {"product_id": MILK, "quantity": 2}, {"product_id": BANANAS, "modality": "DELIVERY"},
    ]})

    result = await call_ok(mcp, "send_draft_to_cart")
    assert add.call_count == 1
    assert add.last_request.json() == {"items": [
        {"upc": MILK, "quantity": 2, "modality": "PICKUP"},
        {"upc": BANANAS, "quantity": 1, "modality": "DELIVERY"},
    ]}
    assert result["summary"]["estimated_total"] == pytest.approx(2 * 2.99 + 0.59)
    assert "warning" not in result
    # The details saved in the draft are reused, not looked up again
    assert lookups == [[MILK, BANANAS]]

    assert (await call_ok(mcp, "view_draft"))["items"] == []
    cart = await call_ok(mcp, "view_current_cart")
    assert [(i["product_id"], i["quantity"]) for i in cart["current_cart"]] == [(MILK, 2), (BANANAS, 1)]
    assert cart["current_cart"][0]["details"]["description"] == "2% Milk"
    assert cart["summary"]["estimated_total"] == pytest.approx(2 * 2.99 + 0.59)


async def test_send_empty_draft(mcp):
    error, _ = await call_error(mcp, "send_draft_to_cart")
    assert error["type"] == "empty_draft"


async def test_send_refuses_to_raise_the_quantity_of_items_already_sent(mcp, requests_mock, lookups):
    save_user_token()
    add = cart_add(requests_mock)
    await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK}]})
    await call_ok(mcp, "send_draft_to_cart")
    await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK}, {"product_id": BANANAS}]})

    view = await call_ok(mcp, "view_draft")
    assert view["already_in_cart"] == [{"product_id": MILK, "modality": "PICKUP", "tracked_quantity": 1}]

    error, text = await call_error(mcp, "send_draft_to_cart")
    assert error["type"] == "quantity_would_increase"
    assert error["items"] == [{"product_id": MILK, "modality": "PICKUP", "tracked_quantity": 1}]
    assert "Nothing was sent" in text
    assert add.call_count == 1
    assert len((await call_ok(mcp, "view_draft"))["items"]) == 2

    await call_ok(mcp, "send_draft_to_cart", {"allow_quantity_increase": True})
    assert add.call_count == 2
    cart = (await call_ok(mcp, "view_current_cart"))["current_cart"]
    assert [(i["product_id"], i["quantity"]) for i in cart] == [(MILK, 2), (BANANAS, 1)]


async def test_send_rejected_by_kroger_keeps_the_draft(mcp, requests_mock, lookups):
    save_user_token()
    requests_mock.put(CART_URL, status_code=400, json=kroger_error("CART-2012", "Quantity must be at least 1"))
    await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK}]})
    error, text = await call_error(mcp, "send_draft_to_cart")
    assert error["type"] == "bad_request"
    assert "The draft was kept" in text
    assert len((await call_ok(mcp, "view_draft"))["items"]) == 1
    assert (await call_ok(mcp, "view_current_cart"))["current_cart"] == []


async def test_send_with_an_unknown_outcome_says_to_check_the_cart(mcp, requests_mock, lookups):
    save_user_token()
    requests_mock.put(CART_URL, exc=requests.exceptions.ReadTimeout("read timed out"))
    await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK}]})
    error, _ = await call_error(mcp, "send_draft_to_cart")
    assert error["outcome_unknown"] is True
    assert error["next_step"].startswith("Do not call send_draft_to_cart again yet")
    assert "clear_draft" in error["next_step"]
    assert len((await call_ok(mcp, "view_draft"))["items"]) == 1


async def test_send_needs_a_sign_in(mcp, lookups):
    await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK}]})
    error, _ = await call_error(mcp, "send_draft_to_cart")
    assert error["type"] == "authentication_required"
    assert len((await call_ok(mcp, "view_draft"))["items"]) == 1


async def test_send_succeeds_when_only_the_local_tracking_fails(mcp, requests_mock, lookups, monkeypatch):
    save_user_token()
    cart_add(requests_mock)
    await call_ok(mcp, "add_to_draft", {"items": [{"product_id": MILK}]})

    def broken(*args):
        raise OSError("disk full")

    monkeypatch.setattr(cart_tools, "_save_cart_data", broken)
    result = await call_ok(mcp, "send_draft_to_cart")
    assert "local cart tracking was not" in result["warning"]
    # The draft is emptied anyway, so a retry cannot send the items twice
    assert (await call_ok(mcp, "view_draft"))["items"] == []


# --- the direct add tools -----------------------------------------------------------


async def test_direct_add_saves_details_and_reports_a_quantity_increase(mcp, requests_mock, lookups):
    save_user_token()
    cart_add(requests_mock)
    first = await call_ok(mcp, "add_items_to_cart", {"product_id": MILK})
    assert "quantity_increased" not in first
    second = await call_ok(mcp, "bulk_add_to_cart", {"items": [{"product_id": MILK}, {"product_id": EGGS}]})
    assert second["quantity_increased"] == [{"product_id": MILK, "modality": "PICKUP", "previously_tracked_quantity": 1}]
    assert EGGS in second["details_warning"]
    cart = (await call_ok(mcp, "view_current_cart"))["current_cart"]
    assert cart[0]["quantity"] == 2
    assert cart[0]["details"]["description"] == "2% Milk"
    assert "details" not in cart[1]


# --- the product lookup -------------------------------------------------------------


def raw_product(product_id, regular, promo=0, sold_by="UNIT"):
    return {
        "productId": product_id,
        "description": f"Product {product_id}",
        "brand": "Kroger",
        "productPageURI": f"/p/some-product/{product_id}?cid=dis.api.tpi_products-api_t:client-id",
        "items": [{"size": "1 gal", "soldBy": sold_by, "price": {"regular": regular, "promo": promo}}],
    }


def test_lookup_product_summaries(requests_mock, app_token):
    search = requests_mock.get(f"{BASE}/v1/products", json={
        "data": [raw_product(MILK, 3.49, 2.99), raw_product(BANANAS, 0.59, sold_by="WEIGHT")],
        "meta": {"warnings": [f"The following productIds were not found: {EGGS}"]},
    })
    summaries, warning = product_tools.lookup_product_summaries([MILK, BANANAS, EGGS, MILK], "70100360")
    assert search.call_count == 1
    query = search.last_request.qs
    assert query["filter.productid"] == [f"{MILK},{BANANAS},{EGGS}"]
    assert query["filter.locationid"] == ["70100360"]
    assert summaries[MILK] == {
        "description": f"Product {MILK}", "brand": "Kroger", "size": "1 gal", "sold_by": "UNIT",
        "regular_price": 3.49, "sale_price": 2.99,
        # The tracking query, which names the app's client ID, is dropped
        "product_url": f"https://www.kroger.com/p/some-product/{MILK}",
        "nutrition": None,
    }
    assert summaries[BANANAS]["sale_price"] is None
    assert warning == f"Kroger has no product details for {EGGS}."


def test_lookup_product_summaries_batches_requests(requests_mock, app_token):
    search = requests_mock.get(f"{BASE}/v1/products", json={"data": []})
    ids = [f"{i:013d}" for i in range(120)]
    summaries, warning = product_tools.lookup_product_summaries(ids, None)
    assert search.call_count == 3
    assert summaries == {}
    assert warning.startswith("Kroger has no product details")


def test_lookup_product_summaries_never_raises(requests_mock, app_token):
    requests_mock.get(f"{BASE}/v1/products", status_code=500, json=kroger_error("PRODUCT-5000", "boom"))
    summaries, warning = product_tools.lookup_product_summaries([MILK], "70100360")
    assert summaries == {}
    assert warning.startswith(f"Product details could not be looked up for {MILK}")
