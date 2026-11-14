import base64
import hashlib
import os
import struct
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from infra.monitor.audit_video import png_frames,prepare


class AuditVideoTests(unittest.TestCase):
    def frame(self):
        return b'\x89PNG\r\n\x1a\n'+struct.pack('>I',0)+b'IEND'+b'0000'

    def test_frames_are_separated_and_truncation_rejected(self):
        frame=self.frame()
        self.assertEqual([frame,frame],png_frames(frame+frame))
        with self.assertRaises(ValueError):png_frames(frame[:-1])
        with self.assertRaises(ValueError):png_frames(b'not-an-image')

    def test_media_checksum_and_no_raw_video_forwarded(self):
        media={'state':'ready','mime_type':'video/webm','data_base64':base64.b64encode(b'clip').decode(),
               'sha256':hashlib.sha256(b'clip').hexdigest()}
        value={'parts':[{'type':'text','text':'source'}],'unsupported_media':[media]}
        with patch('infra.monitor.audit_video.subprocess.run',return_value=SimpleNamespace(returncode=0,stdout=self.frame()))as run:
            output=prepare(value)
        self.assertEqual(3,len(output['parts']));self.assertNotIn('unsupported_media',output)
        self.assertEqual(15,run.call_args.kwargs['timeout'])
        media['sha256']='wrong'
        with self.assertRaises(ValueError):prepare(value)

    def test_unavailable_audio_is_explicit_not_assumed_chat(self):
        output=prepare({'parts':[],'unsupported_media':[{'state':'unsupported_for_visual_audit'}]})
        self.assertIn('unsupported_for_visual_audit',output['parts'][0]['text'])

    def test_decoder_receives_seekable_memory_and_descriptor_always_closes(self):
        body=b'bounded-video-with-index-at-end'
        media={'state':'ready','mime_type':'video/mp4',
               'data_base64':base64.b64encode(body).decode(),'sha256':hashlib.sha256(body).hexdigest()}
        seen=[]
        def decode(command,**kwargs):
            descriptor=kwargs['pass_fds'][0];seen.append(descriptor)
            self.assertEqual('/proc/self/fd/'+str(descriptor),command[command.index('-i')+1])
            self.assertNotIn('input',kwargs)
            os.lseek(descriptor,-3,os.SEEK_END)
            self.assertEqual(body[-3:],os.read(descriptor,3))
            raise TimeoutError('decoder deadline')
        with patch('infra.monitor.audit_video.subprocess.run',side_effect=decode):
            with self.assertRaises(TimeoutError):prepare({'parts':[],'unsupported_media':[media]})
        with self.assertRaises(OSError):os.fstat(seen[0])
