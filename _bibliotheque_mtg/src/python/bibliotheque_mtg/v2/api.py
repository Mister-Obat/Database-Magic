"""Versioned local API facade used by the local UI and tests."""
from .query import CONTRACT_VERSION, list_editions, search_cards, card_detail, edition_cards


def dispatch(db, route, params=None):
    params=dict(params or {})
    if route=='/api/v2/editions': result=list_editions(db,limit=params.get('limit',50),offset=params.get('offset',0))
    elif route=='/api/v2/search': result=search_cards(db,term=params.get('q',''),set_code=params.get('set'),language=params.get('lang'),rarity=params.get('rarity'),color=params.get('color'),color_mode=params.get('color_mode') or 'contains',mana_value=params.get('mana_value'),type_text=params.get('type'),mana_min=params.get('mana_min'),mana_max=params.get('mana_max'),power_min=params.get('power_min'),power_max=params.get('power_max'),toughness_min=params.get('toughness_min'),toughness_max=params.get('toughness_max'),card_type=params.get('card_type'),image_filter=params.get('image_filter') or 'all',result_sort=params.get('result_sort') or 'date',oracle_id=params.get('oracle_id'),printing_id=params.get('printing_id'),unique=params.get('unique') or False,limit=params.get('limit',50),offset=params.get('offset',0))
    elif route=='/api/v2/card': result=card_detail(db,params.get('id',''))
    elif route=='/api/v2/edition': result=edition_cards(db,params.get('set',''),limit=params.get('limit',50),offset=params.get('offset',0))
    else: result={'contract_version':CONTRACT_VERSION,'error':'route_not_found'}
    return result
