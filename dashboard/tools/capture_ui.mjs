import {chromium} from '../frontend/node_modules/playwright/index.mjs';
import fs from 'node:fs/promises';
const [url,out,mode='replay']=process.argv.slice(2);
const browser=await chromium.launch({args:['--no-sandbox','--disable-dev-shm-usage']});
try{
 const page=await browser.newPage({viewport:{width:1440,height:1100}});
 await page.goto(url);await page.getByRole('heading',{name:'KETRIS 자원 관측'}).waitFor();
 if(mode==='live'){
  await page.waitForTimeout(1800);await page.screenshot({path:out+'/live-mig.png',fullPage:true});
  const text=await page.locator('body').innerText();await fs.writeFile(out+'/live-page.txt',text);
  if(!text.includes('22 SM')||!text.includes('일치 확인'))throw new Error('live MIG observation not visible');
 }else{
  await page.getByRole('button',{name:'실험 재생',exact:true}).click();
  for(const [label,condition,name] of [['MIG · 2026.10.12','space50_50','mig-space'],['MIG · 2026.10.12','time3_1','mig-time'],['MIG · 2026.10.12','combined50_50_time3_1','mig-combined'],['RNGD · 2026.10.12','spacetime_70_30','rngd-combined'],['MIG · 2026.10.13','combined50_50_time3_1','mig-new-runtime']]){
   await page.getByLabel('실험',{exact:true}).selectOption({label});await page.waitForTimeout(250);
   await page.getByLabel('조건').selectOption(condition);await page.waitForTimeout(100);
   await page.screenshot({path:out+'/'+name+'.png',fullPage:true});
  }
  await page.getByRole('button',{name:'원본 증빙',exact:true}).click();await page.getByRole('dialog').waitFor();
  await page.screenshot({path:out+'/evidence.png',fullPage:true});await page.keyboard.press('Escape');
  await page.setViewportSize({width:1024,height:900});await page.screenshot({path:out+'/responsive-1024.png',fullPage:true});
 }
}finally{await browser.close()}
