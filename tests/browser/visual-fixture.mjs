// Frozen synthetic design evidence, never production or source-site parity evidence.
export const instant='2026-10-26T11:11:00Z';
export const quote={quote_id:'visual-quote',market_name:'亚洲让球',market_kind:'handicap',period:'full_time',
  line:'-0.25',selection:'home',price:'1.96',status:'open',is_main:true,fresh:true,source_updated_at:instant};
export const fixtures=Array.from({length:12},(_,i)=>({id:'visual-match-'+i,sport:'football',
  competition_id:'visual-league-'+i%3,competition_name:['英格兰超级联赛','西班牙甲级联赛','欧洲冠军联赛'][i%3],
  home_team:['利物浦','皇家马德里','国际米兰','阿森纳'][i%4],away_team:['曼城','巴塞罗那','拜仁慕尼黑','切尔西'][i%4],
  phase:i<3?'live':i===7?'finished':'scheduled',match_state:'active',kickoff_at:instant,source_updated_at:instant,
  home_score:i<3?1:null,away_score:i<3?0:null,clock_minute:i<3?63:null,quote_fresh:i!==3,
  markets:[{...quote,fresh:i!==3}]}));
export const expert={id:'visual-expert',display_nickname:'林老师 TG',total_signals:25,settled_signals:20,
  win:10,half_win:2,loss:5,half_loss:1,push:1,void:1,pending:5,effective_stake:16.5,unit_net:3.42,
  accuracy:11/16.5,average_unit_net:.171,unit_roi:3.42/16.5};
export const plans=[{code:'member',name:'专家会员',price:'88',currency:'CNY',duration_days:30,
  entitlements:{expert_full_history:true,expert_live_signals:true,automatic_betting:true},enabled:true}];
export async function fixtureRoute(route){
  const url=new URL(route.request().url()),pathname=url.pathname.replace('/api/v1','');
  if(pathname==='/auth/me'||pathname==='/admin/me')return route.fulfill({status:401,json:{detail:'尚未登录'}});
  if(pathname==='/public/matches'){
    const phase=url.searchParams.get('phase');
    return route.fulfill({json:{items:fixtures.filter(item=>!phase||phase==='all'||item.phase===phase),next_cursor:null,server_time:instant}});
  }
  if(pathname==='/public/leagues')return route.fulfill({json:{items:[{competition_id:'visual-league-0',competition_name:'英格兰超级联赛',match_count:4}]}});
  if(pathname==='/public/experts')return route.fulfill({json:{items:[expert],next_cursor:null}});
  if(pathname==='/public/signals')return route.fulfill({json:{items:[],next_cursor:null,limited:true}});
  if(pathname==='/public/membership/plans')return route.fulfill({json:{items:plans,payment_available:false}});
  if(pathname==='/public/matches/visual-match-0')return route.fulfill({json:{fixture:fixtures[0],markets:[quote,
    {...quote,quote_id:'visual-away',selection:'away',line:'0.25',price:'1.90'},
    {...quote,quote_id:'visual-additional',is_main:false,line:'-0.5',price:'2.18'}],expert_signals:[]}});
  // Unknown data endpoints fail visibly; the baseline cannot silently invent a shape.
  return route.fulfill({status:404,json:{detail:'冻结视觉样本未包含此接口'}});
}
