from mtgsync.square_build import ImportRow
from mtgsync.square_catalog import apply_catalog, plan_catalog


def _row(item, var, sku, qty=1, price=None):
    return ImportRow(item, var, sku, price, qty, description="Instant", category="MTG Singles")


EXISTING = [{"id": "ITEM_BOLT", "item_data": {"name": "Lightning Bolt", "variations": [
    {"id": "V1", "item_variation_data": {"sku": "sta-42-nf-nm"}}]}},
    {"id": "ITEM_MOAT", "item_data": {"name": "Moat", "variations": []}}]
CATS = [{"id": "CAT1", "category_data": {"name": "MTG Singles"}}]


def test_plan_matches_by_sku_then_name_then_creates():
    rows = [_row("Lightning Bolt", "v nm", "sta-42-nf-nm", 3),       # exists -> untouched
            _row("Lightning Bolt", "v foil", "sta-42-f-nm", 1),       # new variation on existing item (by SKU)
            _row("Moat", "v", "leg-28-nf-lp", 1),                     # new variation on existing item (by name)
            _row("New Card", "v a", "abc-1-nf-nm", 2, price=1.5),     # new item with two variations
            _row("New Card", "v b", "abc-1-f-nm", 1)]
    plan = plan_catalog(rows, EXISTING, CATS, "MTG Singles", alert_count=4)
    assert plan.category_id == "CAT1"
    assert plan.existing_variations == 1
    assert plan.new_item_names == ["New Card"] and plan.items_extended == ["Lightning Bolt", "Moat"]
    assert [v["item_variation_data"]["item_id"] for v in plan.new_variations] == ["ITEM_BOLT", "ITEM_MOAT"]
    assert plan.new_variations[0]["item_variation_data"]["ordinal"] == 2      # after the existing one
    item = plan.new_items[0]
    assert item["item_data"]["name"] == "New Card" and item["item_data"]["categories"] == [{"id": "CAT1"}]
    assert item["item_data"]["description_html"] == "<p>Instant</p>"
    v = item["item_data"]["variations"][0]["item_variation_data"]
    assert v["item_id"] == item["id"] and v["pricing_type"] == "FIXED_PRICING"
    assert v["price_money"] == {"amount": 150, "currency": "USD"}
    assert v["track_inventory"] is True and v["inventory_alert_threshold"] == 4
    assert item["item_data"]["variations"][1]["item_variation_data"]["pricing_type"] == "VARIABLE_PRICING"
    assert plan.new_variation_count == 4


class FakeClient:
    def __init__(self):
        self.calls: list[list[dict]] = []

    def upsert_objects(self, objects):
        self.calls.append(objects)
        if objects and objects[0]["type"] == "CATEGORY":
            return {"id_mappings": [{"client_object_id": "#cat", "object_id": "CAT_NEW"}]}
        return {}


def test_apply_creates_category_first_and_rewrites_references():
    rows = [_row("New Card", "v", "abc-1-nf-nm", 2)]
    plan = plan_catalog(rows, [], [], "MTG Singles", alert_count=None)
    assert plan.category_id is None and "categories" not in plan.new_items[0]["item_data"]
    client = FakeClient()
    stats = apply_catalog(client, plan, log=lambda m: None)
    assert stats == {"category_created": 1, "items_created": 1, "variations_created": 1}
    assert client.calls[0][0]["type"] == "CATEGORY"
    assert client.calls[1][0]["item_data"]["categories"] == [{"id": "CAT_NEW"}]
    assert client.calls[1][0]["item_data"]["variations"][0]["item_variation_data"]["inventory_alert_type"] == "NONE"
