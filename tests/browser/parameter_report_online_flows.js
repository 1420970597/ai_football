async page => {
  await page.unrouteAll({behavior:'wait'});
  await page.goto('file:///root/ai_football/reports/runtime-parameter-optimization/REPORT.html?online='+Date.now());
  await page.waitForFunction(()=>document.querySelectorAll('pre.mermaid svg').length===2,{timeout:25000});
  const diagrams=await page.locator('pre.mermaid svg').count();
  if(diagrams!==2) throw new Error('both online Mermaid diagrams must render');
  await page.screenshot({path:'/root/ai_football/output/playwright/runtime-parameters-report-online.png',fullPage:true});
  return {onlineRenderedDiagrams:diagrams};
}
