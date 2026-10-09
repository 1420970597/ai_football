import unittest

from service.llm import LLMClient, LLMError


class GatewayEnvelopeTests(unittest.TestCase):
    def test_explicit_success_wrapper_and_plain_completion(self):
        completion={'choices':[{'message':{'content':'{"verdict":"confirm","confidence":0.6}'}}]}
        self.assertEqual(LLMClient._content_of(completion),LLMClient._content_of({'success':True,'data':completion}))

    def test_error_wrapper_is_never_an_answer(self):
        completion={'choices':[{'message':{'content':'pretend answer'}}]}
        for response in ({'success':False,'data':completion}, {'success':'true','data':completion}, {'success':True,'data':[]}):
            with self.assertRaises(LLMError):
                LLMClient._content_of(response)

    def test_reasoning_without_answer_remains_error(self):
        with self.assertRaises(LLMError):
            LLMClient._content_of({'success':True,'data':{'choices':[{'message':{'reasoning_content':'thinking','content':''},'finish_reason':'length'}]}})
