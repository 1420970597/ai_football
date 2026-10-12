async page => {
  await page.route('https://cdn.jsdelivr.net/**',route=>route.abort());
  await page.goto('file:///root/ai_football/reports/runtime-parameter-optimization/REPORT.html');
  const text=await page.locator('body').textContent();
  if(!text.includes('参数清单：保持')||!text.includes('前瞻')||!text.includes('30场约17.9pp')) throw new Error('report incomplete');
  if(await page.locator('pre.mermaid').count()!==2) throw new Error('Mermaid fallback missing');
  if(await page.locator('table').count()<3) throw new Error('comparison/source/parameter tables missing');
  const chart=page.locator('img');
  if(await chart.count()!==1||!(await chart.evaluate(image=>image.complete&&image.naturalWidth>0))) throw new Error('sensitivity figure failed to load');
  await page.screenshot({path:'/root/ai_football/output/playwright/runtime-parameters-report.png',fullPage:true});
  return {tables:await page.locator('table').count(),mermaidFallback:await page.locator('pre.mermaid').count(),offlineReadable:true};
}
