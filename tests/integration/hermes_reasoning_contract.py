"""Run in the pinned Hermes image; no provider, tools, jobs or money are used."""
import copy
from pathlib import Path
import sys
import unittest

from ruamel.yaml import YAML
from hermes_constants import resolve_reasoning_config
from providers import get_provider_profile
from agent.transports.chat_completions import ChatCompletionsTransport

CONFIG = Path(sys.argv.pop(1)) if len(sys.argv) > 1 else Path('/source/integrations/hermes/config.yaml')


class HermesReasoningContract(unittest.TestCase):
    def setUp(self):
        self.config = YAML(typ='safe').load(CONFIG.read_text())

    def test_known_model_spellings_explicitly_disable_reasoning(self):
        for model in ('deepseek-v4.1-flash', 'cline-pass/deepseek-v4.1-flash',
                      'custom:cline/deepseek-v4.1-flash'):
            with self.subTest(model=model):
                self.assertEqual({'enabled': False}, resolve_reasoning_config(self.config, model))

    def test_unrelated_model_keeps_its_existing_default(self):
        self.assertIsNone(resolve_reasoning_config(self.config, 'isolated-unrelated-model'))

    def test_native_custom_transport_preserves_visual_input_and_tools(self):
        model = 'deepseek-v4.1-flash'
        profile = get_provider_profile('custom')
        self.assertIsNotNone(profile)
        messages = [{'role': 'user', 'content': [
            {'type': 'text', 'text': 'isolated visual protocol fixture'},
            {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,aXNvbGF0ZWQ=', 'detail': 'high'}},
        ]}]
        tools = [{'type': 'function', 'function': {
            'name': 'mcp__football__search_matches', 'description': 'isolated schema fixture',
            'parameters': {'type': 'object', 'properties': {}, 'required': []},
        }}]
        original = copy.deepcopy((messages, tools))
        transport = ChatCompletionsTransport()
        options = dict(model=model, messages=messages, tools=tools, provider_profile=profile,
                       provider_name='custom', is_custom_provider=True,
                       base_url='https://isolated.invalid/v1', timeout=30)
        before = transport.build_kwargs(**options, reasoning_config=profile.default_reasoning_config(model))
        after = transport.build_kwargs(**options, reasoning_config=resolve_reasoning_config(self.config, model))
        self.assertEqual('medium', before.pop('reasoning_effort', None))
        self.assertEqual('none', after.pop('reasoning_effort', None))
        self.assertEqual(before, after)
        self.assertEqual(original, (messages, tools))
        self.assertEqual(messages, after['messages'])
        self.assertEqual(tools, after['tools'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
