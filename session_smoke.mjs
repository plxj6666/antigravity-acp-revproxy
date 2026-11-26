// Use a local installation of the official OpenAI SDK and a loopback-only test gateway.
import {randomBytes} from 'node:crypto';
const {default:OpenAI}=await import(process.env.OPENAI_SDK_PATH);
const client=new OpenAI({baseURL:`http://127.0.0.1:${process.env.PORT}/v1`,apiKey:process.env.GATEWAY_API_KEY,maxRetries:0,timeout:120000});
for(const n of (process.env.ONLY_THOUGHT?[]:[1,2])){
 const names=['first_value','second_value'].slice(0,n), values=names.map(()=>randomBytes(6).toString('hex'));
 const tools=names.map(name=>({type:'function',function:{name,description:'Return a private client value. Call once.',parameters:{type:'object',properties:{}}}}));
 const messages=[{role:'user',content:`Call ${names.join(' and ')} exactly once each in this turn, then return their values in the order listed. Do not guess.`}];
 const seen=new Set();
 for(let i=0;i<=n;i++){
   const {data,response}=await client.chat.completions.create({model:'gemini-4-neon',messages,tools}).withResponse();
   const m=data.choices[0].message;console.log(JSON.stringify({n,step:i,mode:response.headers.get('x-acp-session-state'),tool:!!m.tool_calls?.length,billed:!!data.usage}));
   if(i>0 && response.headers.get('x-acp-session-state')!=='tool')throw Error('Cold replay');
   if(!m.tool_calls?.length){if(seen.size!==n || !values.every(v=>m.content?.includes(v)) || !data.usage)throw Error('Wrong final settlement');break;}
   const c=m.tool_calls[0],j=names.indexOf(c.function.name);if(j<0||seen.has(j)||i===n)throw Error('Unexpected tool');seen.add(j);
   messages.push({role:'assistant',content:m.content,tool_calls:m.tool_calls},{role:'tool',tool_call_id:c.id,content:values[j]});
 }
}
const stream=await client.chat.completions.create({stream:true,model:'gemini-4-neon',reasoning_effort:'high',messages:[{role:'user',content:'Three boxes have labels AB, AA, BB. Each label is wrong. You draw A from the box labeled AB. Determine all three contents. Explain the deduction and then check that every label is wrong.'}]});
let thought=0,text=0;try{for await(const x of stream){if(x.choices[0]?.delta?.reasoning_content)thought++;if(x.choices[0]?.delta?.content)text++;}}finally{console.log(JSON.stringify({high_summary_frames:thought,text_frames:text}));}
