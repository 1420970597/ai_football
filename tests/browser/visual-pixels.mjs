import zlib from 'node:zlib';
export function decodePng(input){
  if(input.subarray(0,8).toString('hex')!=='89504e470d0a1a0a')throw Error('invalid PNG');
  let offset=8,width,height,channels;const chunks=[];
  while(offset<input.length){
    const length=input.readUInt32BE(offset),type=input.toString('ascii',offset+4,offset+8),data=input.subarray(offset+8,offset+8+length);
    if(offset+12+length>input.length)throw Error('truncated PNG');
    if(type==='IHDR'){
      width=data.readUInt32BE(0);height=data.readUInt32BE(4);
      if(data[8]!==8||![2,6].includes(data[9])||data[12]!==0||width*height>10000000)throw Error('unsupported bounded PNG');
      channels=data[9]===2?3:4;
    }
    if(type==='IDAT')chunks.push(data);
    offset+=12+length;
    if(type==='IEND')break;
  }
  if(!width||!height||!channels)throw Error('missing PNG dimensions');
  const stride=width*channels,raw=zlib.inflateSync(Buffer.concat(chunks),{maxOutputLength:(stride+1)*height});
  if(raw.length!==(stride+1)*height)throw Error('invalid PNG scanline length');
  const decoded=Buffer.alloc(stride*height),pixels=Buffer.alloc(width*height*4,255);
  const paeth=(a,b,c)=>{const p=a+b-c,pa=Math.abs(p-a),pb=Math.abs(p-b),pc=Math.abs(p-c);return pa<=pb&&pa<=pc?a:pb<=pc?b:c;};
  for(let y=0;y<height;y++){
    const filter=raw[y*(stride+1)];if(filter>4)throw Error('unsupported PNG filter');
    for(let x=0;x<stride;x++){
      const i=y*stride+x,a=x>=channels?decoded[i-channels]:0,b=y?decoded[i-stride]:0,c=y&&x>=channels?decoded[i-stride-channels]:0;
      decoded[i]=(raw[y*(stride+1)+1+x]+[0,a,b,Math.floor((a+b)/2),paeth(a,b,c)][filter])&255;
    }
  }
  for(let i=0;i<width*height;i++)for(let c=0;c<channels;c++)pixels[i*4+c]=decoded[i*channels+c];
  return {width,height,pixels};
}
export function comparePixels(expected,actual,{channelThreshold=16,maxChangedRatio=.002}={}){
  if(expected.width!==actual.width||expected.height!==actual.height)return {passed:false,reason:'dimensions_changed',expected:[expected.width,expected.height],actual:[actual.width,actual.height]};
  let changed=0,maxDelta=0,minX=actual.width,minY=actual.height,maxX=-1,maxY=-1;
  for(let pixel=0;pixel<actual.width*actual.height;pixel++){
    let delta=0;for(let c=0;c<4;c++)delta=Math.max(delta,Math.abs(expected.pixels[pixel*4+c]-actual.pixels[pixel*4+c]));
    maxDelta=Math.max(maxDelta,delta);
    if(delta>channelThreshold){changed++;const x=pixel%actual.width,y=Math.floor(pixel/actual.width);minX=Math.min(minX,x);maxX=Math.max(maxX,x);minY=Math.min(minY,y);maxY=Math.max(maxY,y);}
  }
  const ratio=changed/(actual.width*actual.height);
  return {passed:ratio<=maxChangedRatio,changed_pixels:changed,changed_ratio:ratio,max_channel_delta:maxDelta,
    changed_bounds:changed?{x:minX,y:minY,width:maxX-minX+1,height:maxY-minY+1}:null,channel_threshold:channelThreshold,max_changed_ratio:maxChangedRatio};
}
