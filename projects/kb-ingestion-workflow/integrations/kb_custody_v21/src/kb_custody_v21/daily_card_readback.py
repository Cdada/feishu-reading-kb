"""Compare fixed P40 cards with the platform's documented readback channel.

The real Card 2.0 GET returns a normalized display definition: generated element
IDs/defaults are added, while callback behaviors and some presentation fields
are omitted. This verifies display only. Action authority remains the frozen
send request plus the separately checked owner/message/card-ID callback.
"""
from copy import deepcopy
import re


def card_matches(actual, expected):
    if actual == expected:
        return True
    if not isinstance(actual, dict):
        return False
    elements = actual.get('body', {}).get('elements') if isinstance(actual.get('body'), dict) else None
    if not isinstance(elements, list):
        return False
    identifiers = []
    for element in elements:
        identifier = element.get('element_id') if isinstance(element, dict) else None
        if not isinstance(identifier, str) or not re.fullmatch(r'_[0-9]{1,20}', identifier):
            return False
        identifiers.append(identifier)
    if len(set(identifiers)) != len(identifiers):
        return False
    observed = deepcopy(actual)
    for element in observed['body']['elements']:
        del element['element_id']

    # Exactly the observed platform profile, not a general field-ignore list.
    # Unknown fields, changed text, extra buttons and retained/changed behaviors
    # still fail comparison. Never infer callback values from the display GET.
    projected = deepcopy(expected)
    projected['config'] = dict(width_mode='default', enable_forward_interaction=False, streaming_mode=False)
    del projected['header']['icon']
    del projected['body']['vertical_spacing']
    for element in projected['body']['elements']:
        if element['tag'] == 'div':
            element['text'].pop('text_size', None)
            element['text']['text_align'] = 'left'
            element['width'] = 'fill'
        elif element['tag'] == 'button':
            del element['behaviors']
    return observed == projected
