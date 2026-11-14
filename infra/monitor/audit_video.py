"""Decode bounded Telegram audit video in Hermes memory; never create signals."""
import base64
import hashlib
import json
import os
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
        # Ordinary MP4 may keep its index at the end and require seeking.
        # A pipe silently loses those frames. A Linux memory file provides
        # seeking without writing original video to persistent disk.
        descriptor=os.memfd_create('telegram-audit-video',flags=os.MFD_CLOEXEC)
        try:
            offset=0
            while offset<len(body): offset+=os.write(descriptor,body[offset:])
            os.lseek(descriptor,0,os.SEEK_SET)
            probe=subprocess.run(['ffprobe','-v','error','-show_entries','format=duration',
                '-of','json','/proc/self/fd/'+str(descriptor)],pass_fds=(descriptor,),
                capture_output=True,timeout=5)
            duration=None
            if probe.returncode==0 and len(probe.stdout)<4096:
                try:
                    candidate=float(json.loads(probe.stdout).get('format',{}).get('duration',0))
                    if 0<candidate<float('inf'):duration=candidate
                except (ValueError,TypeError):pass
            os.lseek(descriptor,0,os.SEEK_SET)
            result=subprocess.run(['ffmpeg','-v','error','-threads','1','-i',
                '/proc/self/fd/'+str(descriptor),'-t','12',
                '-vf','fps=2,scale=768:768:force_original_aspect_ratio=decrease',
                '-frames:v','24','-threads','1','-f','image2pipe','-vcodec','png','pipe:1'],
                pass_fds=(descriptor,),capture_output=True,timeout=15)
        finally:
            os.close(descriptor)
        if result.returncode or len(result.stdout)>24*1024*1024:raise ValueError('video_decode_failed')
        frames=png_frames(result.stdout)
        notes.append({'state':'sampled_video','frames':len(frames),'interval_seconds':.5,
                      'maximum_seconds':12,'duration_seconds':duration,
                      'truncated_or_unknown':duration is None or duration>12,
                      'sha256':media['sha256']})
        for frame in frames:
            parts.append({'type':'image_url','image_url':{'url':'data:image/png;base64,'+
                base64.b64encode(frame).decode(),'detail':'high'}})
    if notes:parts.insert(1,{'type':'text','text':json.dumps({'independent_media_audit':notes})})
    return {'parts':parts}
