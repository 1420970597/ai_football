"""Decode bounded Telegram audit video in Hermes memory; never create signals."""
import base64
import hashlib
import json
import struct
import subprocess


def png_frames(body):
    frames=[];offset=0
    while offset<len(body):
        start=offset
        if body[offset:offset+8]!=b'\x89PNG\r\n\x1a\n':raise ValueError('invalid_frame')
        offset+=8
        while offset<len(body):
            size=struct.unpack('>I',body[offset:offset+4])[0]
            kind=body[offset+4:offset+8];offset+=12+size
            if offset>len(body):raise ValueError('truncated_frame')
            if kind==b'IEND':break
        frames.append(body[start:offset])
    return frames


def prepare(value):
    parts=list(value['parts']);notes=[]
    for media in value.get('unsupported_media')or[]:
        if media.get('state')!='ready':notes.append({'state':media.get('state')});continue
        if media.get('mime_type')not in {'video/webm','video/mp4'}:
            notes.append({'state':'unsupported_visual_format'});continue
        body=base64.b64decode(media['data_base64'],validate=True)
        if len(body)>8*1024*1024 or hashlib.sha256(body).hexdigest()!=media['sha256']:
            raise ValueError('invalid_media_bytes')
        result=subprocess.run(['ffmpeg','-v','error','-threads','1','-i','pipe:0','-t','12',
            '-vf','fps=2,scale=768:768:force_original_aspect_ratio=decrease',
            '-frames:v','24','-threads','1','-f','image2pipe','-vcodec','png','pipe:1'],
            input=body,capture_output=True,timeout=15)
        if result.returncode or len(result.stdout)>24*1024*1024:raise ValueError('video_decode_failed')
        frames=png_frames(result.stdout)
        notes.append({'state':'sampled_video','frames':len(frames),'interval_seconds':.5,
                      'maximum_seconds':12,'sha256':media['sha256']})
        for frame in frames:
            parts.append({'type':'image_url','image_url':{'url':'data:image/png;base64,'+
                base64.b64encode(frame).decode(),'detail':'high'}})
    if notes:parts.insert(1,{'type':'text','text':json.dumps({'independent_media_audit':notes})})
    return {'parts':parts}
