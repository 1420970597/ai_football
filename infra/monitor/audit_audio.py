"""Bounded local audio evidence for independent audits; never create signals."""
import base64
import hashlib
import io
import json
import math
import unicodedata

MAX_BYTES=8*1024*1024


def audio_body(media):
    if media.get('state')!='ready' or media.get('mime_type')!='audio/ogg':
        raise ValueError('audio_not_ready')
    body=base64.b64decode(media['data_base64'],validate=True)
    if not 0<len(body)<=MAX_BYTES or not body.startswith(b'OggS'):
        raise ValueError('invalid_audio_bytes')
    if hashlib.sha256(body).hexdigest()!=media.get('sha256'):
        raise ValueError('audio_checksum')
    return body


def normalized(text):
    return ''.join(c for c in unicodedata.normalize('NFKC',text).casefold()
                   if c.isalnum())


def checked_transcript(media,result):
    audio_body(media)
    if result.get('sha256')!=media['sha256']:
        raise ValueError('transcript_checksum')
    duration=result.get('duration')
    if (not isinstance(duration,(int,float)) or isinstance(duration,bool)
            or not math.isfinite(duration) or not 0<duration<=30):
        return {'state':'audio_uncertain','reason':'duration_outside_audit_budget'}
    reads=result.get('reads')
    if not isinstance(reads,list)or len(reads)!=2:
        raise ValueError('two_audio_decodes_required')
    texts=[]
    for read in reads:
        segments=read.get('segments')
        if (read.get('language')!='zh' or read.get('language_probability',0)<.9
                or not isinstance(segments,list)or not 0<len(segments)<=100):
            return {'state':'audio_uncertain','reason':'language_or_speech_uncertain'}
        text=''.join(s.get('text','')for s in segments).strip()
        if not text or len(text)>4000:
            return {'state':'audio_uncertain','reason':'empty_or_excessive_transcript'}
        for s in segments:
            metrics=[s.get(k)for k in ('start','end','avg_logprob','no_speech_prob','compression_ratio')]
            if any(type(x)not in (int,float)or not math.isfinite(x)for x in metrics):
                raise ValueError('invalid_audio_confidence')
            if (not 0<=s['start']<s['end']<=duration+.1 or s['avg_logprob']<-.7
                    or s['no_speech_prob']>.1 or s['compression_ratio']>2.4):
                return {'state':'audio_uncertain','reason':'low_acoustic_confidence'}
        texts.append(text)
    if normalized(texts[0])!=normalized(texts[1]):
        return {'state':'audio_uncertain','reason':'decodes_disagree'}
    return {'state':'transcribed_audio','sha256':media['sha256'],'duration_seconds':duration,
            'text':texts[0],'machine_transcript':True,'matching_decodes':2,
            'model_revision':result['model_revision']}


def transcribe(media):
    """Run only in the separate no-network local-STT Docker container."""
    import av
    from faster_whisper import WhisperModel
    body=audio_body(media)
    with av.open(io.BytesIO(body))as container:
        duration=float(container.duration/av.time_base)if container.duration else None
    if duration is None or not 0<duration<=30:
        return {'sha256':media['sha256'],'duration':duration,'reads':[]}
    model=WhisperModel('/model',device='cpu',compute_type='int8',cpu_threads=1,
                       num_workers=1,local_files_only=True)
    reads=[]
    for beam in (5,1):
        segments,info=model.transcribe(io.BytesIO(body),beam_size=beam,
            initial_prompt='以下为简体中文口语原文，逐字转写。',
            condition_on_previous_text=False,vad_filter=False,temperature=0)
        reads.append({'language':info.language,'language_probability':info.language_probability,
            'segments':[{'start':s.start,'end':s.end,'text':s.text,'avg_logprob':s.avg_logprob,
                         'no_speech_prob':s.no_speech_prob,'compression_ratio':s.compression_ratio}
                        for s in segments]})
    return {'sha256':media['sha256'],'duration':duration,'reads':reads,
            'model_revision':'Systran/faster-whisper-small@536b0662742c02347bc0e980a01041f333bce120'}


if __name__=='__main__':
    import sys
    print(json.dumps(transcribe(json.load(sys.stdin)),ensure_ascii=False))
