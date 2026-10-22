import {test,expect} from '@playwright/test';
test('actual MIG and RNGD archives, sparse playback, evidence and keyboard',async({page})=>{
 await page.goto('/');await expect(page.getByRole('heading',{name:'KETRIS 자원 관측'})).toBeVisible();
 await page.getByRole('button',{name:'실험 재생',exact:true}).click();
 await page.getByLabel('실험',{exact:true}).selectOption({label:'MIG · 2026.10.12'});
 await page.getByLabel('조건').selectOption('combined50_50_time3_1');
 await expect(page.getByText('74.940%',{exact:true})).toBeVisible();
 await expect(page.getByText('MOCK 기관 요청',{exact:true})).toBeVisible();
 await expect(page.getByText('과거 파티션',{exact:false}).first()).toBeVisible();
 await expect(page.getByText('22 SM',{exact:true}).first()).toBeVisible();
 await page.getByRole('button',{name:'재생 시작',exact:true}).click();await page.waitForTimeout(1200);
 await page.getByRole('button',{name:'일시정지',exact:true}).click();
 await page.getByLabel('측정 창 위치').focus();await page.keyboard.press('Home');
 await page.getByRole('button',{name:'원본 증빙',exact:true}).click();await expect(page.getByRole('dialog')).toContainText('sha256');
 await page.keyboard.press('Escape');await expect(page.getByRole('dialog')).toHaveCount(0);
 await page.getByLabel('실험',{exact:true}).selectOption({label:'RNGD · 2026.10.12'});
 await page.getByLabel('조건').selectOption('spacetime_70_30');
 await page.getByRole('button',{name:'A 상세 보기',exact:true}).click();
 await expect(page.getByText('완료 지연 p95').first()).toBeVisible();
 await expect(page.getByText('70:30 목표 정밀도 재현 실패')).toBeVisible();
 await page.setViewportSize({width:1024,height:900});
 expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
});

test('single resource board follows the selected real measurement window and block identity',async({page})=>{
 await page.goto('/');
 await page.getByRole('button',{name:'실험 재생',exact:true}).click();
 await page.getByLabel('실험',{exact:true}).selectOption({label:'MIG · 2026.10.13'});
 await page.getByLabel('조건').selectOption('combined50_50_time3_1');
 const board=page.getByRole('region',{name:'선택한 시점의 자원 상태'});
 await expect(board).toHaveCount(1);
 await page.getByRole('button',{name:'A 상세 보기',exact:true}).click();
 const detail=page.getByRole('complementary',{name:'워크로드 상세'});
 await expect(detail).toContainText('933');
 await page.getByRole('button',{name:'다음 측정 창',exact:true}).click();
 await expect(board).toHaveCount(1);
 await expect(detail).not.toContainText('933 kernels');
 await page.getByLabel('측정 창 위치').focus();await page.keyboard.press('Home');
 await expect(detail).toContainText('933');
 await page.getByLabel('실험',{exact:true}).selectOption({label:'RNGD · 2026.10.12'});
 await page.getByLabel('조건').selectOption('space_only');
 await expect(board.getByText('PE 7',{exact:true})).toBeVisible();
 await expect(detail).toContainText('자원판에서 워크로드를 선택하세요');
 for(const width of [1440,1024,390]){
  await page.setViewportSize({width,height:900});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
 }
});

test('live history selection stays fixed and can return to latest',async({page})=>{
 await page.goto('/');
 const range=page.getByLabel('관측 시점 위치');
 await expect(range).toBeEnabled();
 await range.focus();await page.keyboard.press('Home');
 await expect(page.getByText('과거 관측 · 선택 고정',{exact:true})).toBeVisible();
 const board=page.getByRole('region',{name:'선택한 시점의 자원 상태'});
 const selected=await board.locator('.selected-time').innerText();
 await page.waitForTimeout(2200);
 expect(await board.locator('.selected-time').innerText()).toBe(selected);
 await page.getByRole('button',{name:'최신 관측 따라가기'}).click();
 await expect(page.getByText('최신 관측 · 자동 갱신',{exact:true})).toBeVisible();
 await expect(board).toHaveCount(1);
});

test('chosen retained time restores all actual MIGs including one without a connected workload',async({page})=>{
 const proofAt=process.env.KETRIS_HISTORY_PROOF_AT;
 test.skip(!proofAt,'Set KETRIS_HISTORY_PROOF_AT for the retained lab snapshot; history expires after 7 days.');
 await page.goto('/');
 await page.getByLabel('GPU 상태 시점 (KST)').fill(proofAt!);
 await page.getByRole('button',{name:'해당 시점 조회',exact:true}).click();
 const board=page.getByRole('region',{name:'선택한 시점의 자원 상태'});
 const map=board.getByRole('group',{name:'GPU 전체 파티션 지도'});
 await expect(map.getByRole('region')).toHaveCount(2);
 await expect(map.getByText('연결된 워크로드 없음',{exact:true})).toBeVisible();
 await expect(map.getByRole('button',{name:'domain-0 상세 보기',exact:true})).toBeVisible();
 await expect(board.locator('.selected-time')).toContainText('15:17:01');
 await map.getByRole('button',{name:'MIG 1g.24gb 자원 상세 · 2',exact:true}).click();
 const detail=page.getByRole('complementary',{name:'워크로드 상세'});
 await expect(detail).toContainText('0개');
 await expect(detail).toContainText('MIG-67155f7e-c1c5-5cde-a89f-7ee600150005');
 await page.waitForTimeout(2200);
 await expect(map.getByRole('region')).toHaveCount(2);
 await page.getByRole('button',{name:'최신 관측 따라가기',exact:true}).click();
 await expect(board.getByText('Full GPU · MIG Disabled',{exact:true})).toBeVisible();
 await expect(map.getByRole('region')).toHaveCount(1);
 for(const width of [1440,1024,390]){
  await page.setViewportSize({width,height:900});
  expect(await page.evaluate(()=>document.documentElement.scrollWidth<=window.innerWidth)).toBeTruthy();
 }
});
