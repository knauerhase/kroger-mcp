"""
Nutrition facts in a form that can be compared across products

The Products API reports each nutrient with a code (INFOODS tagnames such as
NA for sodium), a quantity, and a unit (GRM, MGM, MC). Products differ in
which nutrients they list, list some without a quantity, and use different
serving sizes, so this module converts each product's nutrition facts to
fixed units, per serving and per 100 g or 100 ml, and keeps track of what is
unknown. An unknown value is never treated as zero.

Phosphorus is rarely reported, so the ingredient list is checked for
phosphate and potassium additives, which matter for kidney (CKD) diets.
"""
import re
from typing import Any, Dict, List, Optional, Tuple

# Nutrient key: (unit it is converted to, Products API codes, display names)
NUTRIENTS: Dict[str, Tuple[str, Tuple[str, ...], Tuple[str, ...]]] = {
    "calories": ("kcal", ("ENER-", "ENERC_KCAL", "ENERC"), ("calories",)),
    "protein_g": ("g", ("PRO-", "PROCNT"), ("protein",)),
    "carbohydrate_g": ("g", ("CHO-", "CHOCDF"), ("total carbohydrate",)),
    "fiber_g": ("g", ("FIBTSW", "FIBTG", "FIBC", "X_FIBTPE"), ("dietary fiber", "dietary fiber, total")),
    "sugar_g": ("g", ("SUGAR", "SUGAR-"), ("sugar", "sugars", "total sugars")),
    "added_sugar_g": ("g", ("SUGAD",), ("added sugar", "added sugars")),
    "fat_g": ("g", ("FAT", "FATNLEA"), ("total fat",)),
    "saturated_fat_g": ("g", ("FASAT",), ("saturated fat",)),
    "trans_fat_g": ("g", ("FATRN",), ("trans fat",)),
    "cholesterol_mg": ("mg", ("CHOL-", "CHOLE"), ("cholesterol",)),
    "sodium_mg": ("mg", ("NA",), ("sodium",)),
    "potassium_mg": ("mg", ("K",), ("potassium",)),
    "phosphorus_mg": ("mg", ("P",), ("phosphorus",)),
    "calcium_mg": ("mg", ("CA",), ("calcium",)),
    "iron_mg": ("mg", ("FE",), ("iron",)),
}

# The nutrients compare_products can sort by
NUTRIENT_KEYS = tuple(NUTRIENTS)

_BY_CODE = {code: key for key, (_, codes, _) in NUTRIENTS.items() for code in codes}
_BY_NAME = {name: key for key, (_, _, names) in NUTRIENTS.items() for name in names}

# Factors to grams, and to milligrams, by Products API unit code
_GRAMS = {"GRM": 1.0, "MGM": 0.001, "MC": 0.000001}

# A serving size in these units converts to grams or millilitres
_SERVING_GRAMS = {"GRM": 1.0, "ONZ": 28.349523, "MGM": 0.001}
_SERVING_ML = {"MLT": 1.0, "FO": 29.57353, "OZA": 29.57353, "G21": 236.5882, "LTR": 1000.0}

# Ingredient words that mark a phosphate or potassium additive
_PHOSPHATE = re.compile(r"phosph", re.IGNORECASE)
_POTASSIUM = re.compile(r"potassium", re.IGNORECASE)


def _panel(product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The product's first nutrition panel, or None"""
    nutrition = product.get("nutritionInformation")
    if isinstance(nutrition, list):
        nutrition = nutrition[0] if nutrition else None
    return nutrition if isinstance(nutrition, dict) else None


def _nutrient_key(nutrient: Dict[str, Any]) -> Optional[str]:
    key = _BY_CODE.get(nutrient.get("code"))
    if key is None:
        key = _BY_NAME.get(str(nutrient.get("displayName") or "").strip().lower())
    return key


def _convert(quantity: Any, unit_code: Optional[str], target: str) -> Optional[float]:
    """A nutrient quantity in the target unit (g, mg, or kcal), or None if it cannot be converted"""
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)):
        return None
    if target == "kcal":
        # Energy is reported in kcal whatever the unit code says (E14, D70, ...)
        return float(quantity)
    factor = _GRAMS.get(unit_code) if unit_code else None
    if factor is None:
        # A missing unit is the usual unit for the nutrient
        factor = 1.0 if target == "g" else 0.001
    return quantity * factor if target == "g" else quantity * factor * 1000


def _serving(panel: Dict[str, Any]) -> Tuple[Optional[str], Optional[float], Optional[str]]:
    """The serving size as text, and as an amount in g or ml (None if neither)"""
    size = panel.get("servingSize") or {}
    quantity = size.get("quantity")
    unit = size.get("unitOfMeasure") or {}
    text = None
    if quantity is not None:
        text = f"{quantity:g} {unit.get('abbreviation') or unit.get('name') or ''}".strip() \
            if isinstance(quantity, (int, float)) else str(quantity)
    if isinstance(quantity, bool) or not isinstance(quantity, (int, float)) or quantity <= 0:
        return text, None, None
    code = unit.get("code")
    if code in _SERVING_GRAMS:
        return text, quantity * _SERVING_GRAMS[code], "g"
    if code in _SERVING_ML:
        return text, quantity * _SERVING_ML[code], "ml"
    return text, None, None


def _ingredients_matching(statement: Optional[str], pattern: re.Pattern) -> List[str]:
    """The ingredients of an ingredient statement that match pattern"""
    if not statement:
        return []
    parts = re.split(r"[,;()\[\]]|\band\b", statement)
    found = []
    for part in parts:
        part = part.strip(" .:*")
        if part and pattern.search(part) and part.lower() not in (f.lower() for f in found):
            found.append(part)
    return found


def _round(value: float) -> float:
    return round(value, 1) if abs(value) >= 1 else round(value, 3)


def nutrition_facts(product: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The product's nutrition facts in fixed units, or None if it has none

    Returns:
        serving: The serving size as printed
        servings_per_package: As the API gives it, or None
        per_serving: Each known nutrient (NUTRIENT_KEYS) per serving
        per_100: The same per 100 g or 100 ml, with basis "g" or "ml", or
            None if the serving size is not in a unit of weight or volume
        unknown: Nutrients the product lists without a quantity, or does not
            list. Unknown is not zero.
        ingredients: The ingredient statement
        phosphate_additives, potassium_additives: Ingredients that are
            phosphate or potassium additives (or contain those words)
        nutritional_rating: Kroger's own rating, if any
    """
    panel = _panel(product)
    if panel is None:
        return None

    per_serving: Dict[str, float] = {}
    for nutrient in panel.get("nutrients") or []:
        if not isinstance(nutrient, dict):
            continue
        key = _nutrient_key(nutrient)
        if key is None or key in per_serving:
            continue
        value = _convert(nutrient.get("quantity"), (nutrient.get("unitOfMeasure") or {}).get("code"), NUTRIENTS[key][0])
        if value is not None:
            per_serving[key] = _round(value)

    serving_text, serving_amount, basis = _serving(panel)
    per_100 = None
    if serving_amount:
        per_100 = {"basis": basis, **{k: _round(v * 100 / serving_amount) for k, v in per_serving.items()}}

    ingredients = panel.get("ingredientStatement")
    return {
        "serving": serving_text,
        "servings_per_package": (panel.get("servingsPerPackage") or {}).get("value"),
        "per_serving": per_serving,
        "per_100": per_100,
        "unknown": [k for k in NUTRIENT_KEYS if k not in per_serving],
        "ingredients": ingredients,
        "phosphate_additives": _ingredients_matching(ingredients, _PHOSPHATE),
        "potassium_additives": _ingredients_matching(ingredients, _POTASSIUM),
        "nutritional_rating": panel.get("nutritionalRating"),
    }


def dietary_info(product: Dict[str, Any]) -> Dict[str, Any]:
    """Allergens and dietary claims of a raw product, leaving out what it does not report"""
    info: Dict[str, Any] = {}
    allergens = [
        {"name": a.get("name"), "level": a.get("levelOfContainmentName")}
        for a in product.get("allergens") or [] if isinstance(a, dict)
    ]
    if allergens:
        info["allergens"] = allergens
    if product.get("allergensDescription"):
        info["allergens_description"] = product["allergensDescription"]
    claims = list(product.get("manufacturerDeclarations") or [])
    if product.get("organicClaimName") not in (None, "", "NO"):
        claims.append(f"Organic ({product['organicClaimName']})")
    if product.get("nonGmo"):
        claims.append("Non-GMO")
    if product.get("certifiedForPassover"):
        claims.append("Certified for Passover")
    if claims:
        info["claims"] = claims
    return info
