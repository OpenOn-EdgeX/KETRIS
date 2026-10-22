export async function get<T>(path:string):Promise<T>{
 const controller=new AbortController();const timeout=setTimeout(()=>controller.abort(),4000);
 try{const response=await fetch('/api/v1/'+path,{signal:controller.signal});if(!response.ok)throw new Error(`조회 실패 (${response.status})`);return await response.json() as T}finally{clearTimeout(timeout)}
}
