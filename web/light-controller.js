/* Confirm by brief continuity or a recent majority; raw model output stays separate. */
(function(root){
  class LightController {
    constructor(){this.confirmMs=300;this.holdMs=600;this.graceMs=900;this.windowMs=1200;this.reset();}
    reset(){this.current=null;this.candidate=null;this.candidateAt=0;this.samples=0;this.recent=[];this.lastChange=-Infinity;this.lastSample=null;this.invalidAt=null;this.status='waiting';}
    clearOutput(){this.current=null;this.candidate=null;this.samples=0;this.recent=[];this.status='waiting';}
    tick(now){
      if(this.lastSample!==null && now-this.lastSample>1000){this.candidate=null;this.samples=0;this.recent=[];}
      this.recent=this.recent.filter(sample=>now-sample.at<=this.windowMs);
      const lost=this.invalidAt!==null?this.invalidAt:this.lastSample===null?now:this.lastSample+1000;
      if(now-lost>=this.graceMs){this.clearOutput();this.status='expired';}
      return this.current;
    }
    accept(p,count,now){
      // Repeated delivery of the same sample must not add votes or advance state.
      if(this.lastSample!==null && now<=this.lastSample)return this.current;
      this.tick(now);
      this.lastSample=now;
      const reliable=count===1&&p&&!p.uncertain;
      if(count!==1)this.recent=[];
      else this.recent.push({at:now,label:reliable?p.label:null});
      if(count!==1||!p||p.uncertain){
        this.candidate=null;this.samples=0;
        if(this.invalidAt===null)this.invalidAt=now;
        this.status=this.current?'holding':'waiting';
        this.tick(now);return this.current;
      }
      this.invalidAt=null;
      if(this.current?.label===p.label){this.current=p;this.candidate=null;this.samples=0;this.status='stable';return this.current;}
      if(this.candidate!==p.label){this.candidate=p.label;this.candidateAt=now;this.samples=1;}else this.samples++;
      this.status=this.current?'holding':'confirming';
      const continuous=now-this.candidateAt>=this.confirmMs && this.samples>=2;
      const votes=this.recent.filter(sample=>sample.label===p.label).length;
      const repeated=votes>=3 && votes/this.recent.length>=.75;
      if((continuous||repeated) && now-this.lastChange>=this.holdMs){
        this.current=p;this.lastChange=now;this.candidate=null;this.samples=0;this.recent=[];this.status='stable';
      }
      return this.current;
    }
  }
  root.LightController=LightController;
  if(typeof module!=='undefined')module.exports=LightController;
})(typeof window!=='undefined'?window:globalThis);
