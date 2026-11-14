"""Nutrition facts in fixed units, and compare_products"""
import pytest

from kroger_mcp.tools import shared
from kroger_mcp.tools.nutrition import dietary_info, nutrition_facts

from conftest import BASE, call_error, call_ok

STORE = "70100360"


def nutrient(code, quantity, unit="GRM", name=None):
    n = {"code": code, "displayName": name, "unitOfMeasure": {"code": unit}}
    if quantity is not None:
        n["quantity"] = quantity
    return n


def product(product_id, nutrients, serving=(50, "GRM", "g"), ingredients="Flour, Water, Salt.", **extra):
    panel = {"ingredientStatement": ingredients, "nutrients": nutrients, "servingsPerPackage": {"value": 10}}
    if serving:
        panel["servingSize"] = {"quantity": serving[0], "unitOfMeasure": {"code": serving[1], "abbreviation": serving[2]}}
    return {
        "productId": product_id,
        "description": f"Bread {product_id}",
        "items": [{"size": "24 oz", "soldBy": "UNIT", "price": {"regular": 4.99}}],
        "nutritionInformation": [panel],
        **extra,
    }


def test_nutrition_facts_converts_units_and_scales_to_100g():
    facts = nutrition_facts(product("0000000000001", [
        nutrient("ENER-", 120, "E14"),
        nutrient("NA", 0.25, "GRM"),        # sodium in grams
        nutrient("K", 90, "MGM"),
        nutrient("PRO-", 4),
        nutrient("VITD", 2, "MC"),           # not compared
        nutrient("FASAT", None),             # listed without a value
        nutrient("XYZ", 3, "MGM", name="Phosphorus"),  # unknown code, known name
    ]))
    assert facts["serving"] == "50 g"
    assert facts["per_serving"] == {
        "calories": 120.0, "sodium_mg": 250.0, "potassium_mg": 90.0, "protein_g": 4.0, "phosphorus_mg": 3.0,
    }
    assert facts["per_100"] == {
        "basis": "g", "calories": 240.0, "sodium_mg": 500.0, "potassium_mg": 180.0, "protein_g": 8.0,
        "phosphorus_mg": 6.0,
    }
    # Listed without a value and not listed at all are both unknown, never zero
    assert "saturated_fat_g" in facts["unknown"]
    assert "fiber_g" in facts["unknown"]
    assert "sodium_mg" not in facts["unknown"]
    assert facts["servings_per_package"] == 10


@pytest.mark.parametrize("serving, basis, factor", [
    ((2, "ONZ", "oz"), "g", 100 / 56.699046),
    ((240, "MLT", "ml"), "ml", 100 / 240),
    ((8, "FO", "fl oz"), "ml", 100 / 236.58824),
    ((1, "G21", "cup"), "ml", 100 / 236.5882),
])
def test_serving_sizes_in_weight_or_volume_units(serving, basis, factor):
    facts = nutrition_facts(product("0000000000001", [nutrient("NA", 100, "MGM")], serving=serving))
    assert facts["per_100"]["basis"] == basis
    assert facts["per_100"]["sodium_mg"] == pytest.approx(100 * factor, abs=0.1)


@pytest.mark.parametrize("serving", [(1, "H87", "piece"), (1, "EA", "each"), None])
def test_serving_sizes_without_weight_or_volume_have_no_per_100(serving):
    facts = nutrition_facts(product("0000000000001", [nutrient("NA", 100, "MGM")], serving=serving))
    assert facts["per_100"] is None
    assert facts["per_serving"] == {"sodium_mg": 100.0}


def test_additives_are_found_in_the_ingredients():
    facts = nutrition_facts(product("0000000000001", [], ingredients=(
        "Enriched Flour (Wheat Flour, Malted Barley Flour), Water, Leavening (Sodium Acid Pyrophosphate, "
        "Monocalcium Phosphate), Salt, Potassium Chloride and Potassium Sorbate (Preservative), "
        "Phosphoric Acid."
    )))
    assert facts["phosphate_additives"] == ["Sodium Acid Pyrophosphate", "Monocalcium Phosphate", "Phosphoric Acid"]
    assert facts["potassium_additives"] == ["Potassium Chloride", "Potassium Sorbate"]


def test_product_without_nutrition_facts():
    assert nutrition_facts({"productId": "0000000000001"}) is None
    assert nutrition_facts({"productId": "0000000000001", "nutritionInformation": []}) is None


def test_dietary_info():
    info = dietary_info({
        "allergens": [{"name": "Wheat", "levelOfContainmentName": "Contains"}],
        "allergensDescription": "Contains wheat.",
        "manufacturerDeclarations": ["Vegan"],
        "organicClaimName": "USDA Organic",
        "nonGmo": True,
    })
    assert info == {
        "allergens": [{"name": "Wheat", "level": "Contains"}],
        "allergens_description": "Contains wheat.",
        "claims": ["Vegan", "Organic (USDA Organic)", "Non-GMO"],
    }
    assert dietary_info({"organicClaimName": "NO"}) == {}


# --- compare_products -----------------------------------------------------------------


BREADS = [
    product("0000000000001", [nutrient("NA", 250, "MGM"), nutrient("K", 50, "MGM")]),              # 500 mg/100 g
    product("0000000000002", [nutrient("NA", 180, "MGM")], serving=(30, "GRM", "g")),             # 600 mg/100 g
    product("0000000000003", [nutrient("NA", None)]),                                              # unknown
    product("0000000000004", [nutrient("NA", 200, "MGM")], ingredients="Flour, Monocalcium Phosphate."),  # 400
    {"productId": "0000000000005", "description": "No label", "items": []},
]


@pytest.fixture
def breads(requests_mock, app_token):
    shared.set_preferred_location_id(STORE)
    return requests_mock.get(f"{BASE}/v1/products", json={"data": BREADS})


async def test_compare_sorts_per_100g_with_unknown_values_last(mcp, breads):
    result = await call_ok(mcp, "compare_products", {"search_term": "sourdough bread", "sort_by": "sodium_mg"})
    assert [p["product_id"] for p in result["products"]] == [
        "0000000000004", "0000000000001", "0000000000002", "0000000000003",
    ]
    assert [p["nutrition"].get("sodium_mg") for p in result["products"]] == [400.0, 500.0, 600.0, None]
    assert result["not_comparable"] == ["0000000000003"]
    assert result["without_nutrition_facts"] == [{"product_id": "0000000000005", "description": "No label"}]
    assert result["products"][0]["phosphate_additives"] == ["Monocalcium Phosphate"]
    assert "ingredients" not in result["products"][0]
    assert "Unknown values are not zero" in result["note"]
    assert breads.last_request.qs["filter.term"] == ["sourdough bread"]


async def test_compare_per_serving_descending_with_ingredients(mcp, breads):
    result = await call_ok(mcp, "compare_products", {
        "search_term": "sourdough bread", "sort_by": "sodium_mg", "descending": True,
        "basis": "per_serving", "include_ingredients": True,
    })
    assert [p["nutrition"].get("sodium_mg") for p in result["products"]] == [250.0, 200.0, 180.0, None]
    assert result["products"][0]["ingredients"] == "Flour, Water, Salt."


async def test_compare_by_product_ids(mcp, breads):
    ids = ["0000000000001", "0000000000002"]
    await call_ok(mcp, "compare_products", {"product_ids": ids})
    assert breads.last_request.qs["filter.productid"] == [",".join(ids)]


@pytest.mark.parametrize("arguments", [{}, {"search_term": "bread", "product_ids": ["0000000000001"]}])
async def test_compare_needs_exactly_one_of_term_and_ids(mcp, arguments):
    error, _ = await call_error(mcp, "compare_products", arguments)
    assert error["type"] == "invalid_parameter"


async def test_compare_rejects_an_unknown_nutrient(mcp):
    error, _ = await call_error(mcp, "compare_products", {"search_term": "bread", "sort_by": "salt"})
    assert error["type"] == "invalid_arguments"


def test_product_summary_saves_nutrition_and_dietary_info_for_drafts():
    from kroger_mcp.tools.product_tools import product_summary

    summary = product_summary({**BREADS[3], "allergens": [{"name": "Wheat", "levelOfContainmentName": "Contains"}]})
    assert summary["nutrition"]["per_serving"] == {"sodium_mg": 200.0}
    assert summary["nutrition"]["phosphate_additives"] == ["Monocalcium Phosphate"]
    assert summary["allergens"] == [{"name": "Wheat", "level": "Contains"}]
