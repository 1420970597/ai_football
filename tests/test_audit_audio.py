import base64
import copy
import hashlib
import unittest
from infra.monitor.audit_audio import audio_body,checked_transcript


class AudioAuditTests(unittest.TestCase):
    def setUp(self):
        body=b'OggS-isolated-audio'
        self.media={'state':'ready','mime_type':'audio/ogg','sha256':hashlib.sha256(body).hexdigest(),
                    'data_base64':base64.b64encode(body).decode()}
        read={'language':'zh','language_probability':.96,'segments':[
            dict(start=0,end=2,text='今天吃饭了。',avg_logprob=-.5,no_speech_prob=.03,compression_ratio=.9)]}
        self.result={'sha256':self.media['sha256'],'duration':3.2,'reads':[read,copy.deepcopy(read)],
                     'model_revision':'isolated-model-revision'}

    def test_matching_confident_full_audio_keeps_original_hash_and_machine_label(self):
        value=checked_transcript(self.media,self.result)
        self.assertEqual('transcribed_audio',value['state'])
        self.assertTrue(value['machine_transcript'])
        self.assertEqual(self.media['sha256'],value['sha256'])
        self.assertEqual(2,value['matching_decodes'])

    def test_checksum_and_decode_count_cannot_be_forged(self):
        for media,result in [(dict(self.media,sha256='bad'),self.result),
                             (self.media,dict(self.result,sha256='bad')),
                             (self.media,dict(self.result,reads=self.result['reads'][:1]))]:
            with self.assertRaises(ValueError):checked_transcript(media,result)

    def test_low_confidence_disagreement_and_long_audio_stay_unknown(self):
        variants=[]
        for key,value in [('avg_logprob',-.71),('no_speech_prob',.11),('compression_ratio',2.5)]:
            result=copy.deepcopy(self.result);result['reads'][0]['segments'][0][key]=value;variants.append(result)
        result=copy.deepcopy(self.result);result['reads'][1]['segments'][0]['text']='甲队大二点五';variants.append(result)
        variants.extend([dict(self.result,duration=31),dict(self.result,duration=None)])
        for result in variants:
            with self.subTest(result=result):
                self.assertEqual('audio_uncertain',checked_transcript(self.media,result)['state'])

    def test_invalid_audio_or_confidence_is_rejected(self):
        with self.assertRaises(ValueError):audio_body(dict(self.media,data_base64='invalid'))
        result=copy.deepcopy(self.result);result['reads'][0]['segments'][0]['avg_logprob']=float('nan')
        with self.assertRaises(ValueError):checked_transcript(self.media,result)
