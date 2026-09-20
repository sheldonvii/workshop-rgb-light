const assert=require('node:assert/strict');
const LightController=require('../web/light-controller.js');
const p=label=>({label,uncertain:false});
const c=new LightController();
const put=(label,t)=>c.accept(p(label),1,t);
const uncertain=t=>c.accept({...p('happy'),uncertain:true},1,t);

put('happy',0);c.tick(300);assert.equal(c.current,null,'one frame never confirms');
put('happy',350);assert.equal(c.current.label,'happy','normal sampling confirms in 350 ms');
put('sad',400);put('sad',700);assert.equal(c.current.label,'happy','cooldown still applies');
put('sad',950);assert.equal(c.current.label,'sad','switch allowed at 600 ms');

// Repeated evidence can confirm even when no continuous run reaches 300 ms.
c.reset();put('happy',0);uncertain(250);put('happy',500);put('happy',750);
assert.equal(c.current.label,'happy','3 of 4 votes tolerate uncertainty');
c.reset();put('happy',0);put('sad',250);put('happy',500);put('happy',750);
assert.equal(c.current.label,'happy','3 of 4 votes tolerate a different label');
c.reset();for(let t=0;t<=3000;t+=250)put(t%500?'sad':'happy',t);
assert.equal(c.current,null,'balanced alternating noise never confirms');
c.reset();put('happy',0);uncertain(250);put('happy',500);uncertain(750);put('happy',1000);
assert.equal(c.current,null,'3 of 5 is insufficient');
c.reset();put('happy',0);uncertain(300);put('happy',600);uncertain(900);uncertain(1200);put('happy',1300);
assert.equal(c.current,null,'expired votes do not count');

c.reset();put('happy',0);put('happy',350);uncertain(400);c.tick(1299);
assert.equal(c.current.label,'happy');c.tick(1300);assert.equal(c.current,null,'900 ms uncertainty expires');
c.reset();put('happy',0);put('happy',350);c.accept(null,0,400);c.tick(1300);
assert.equal(c.current,null,'absence expires');
c.reset();put('happy',0);put('happy',350);c.tick(2250);
assert.equal(c.current,null,'stalled input expires without another response');
c.reset();put('happy',0);put('happy',1100);assert.equal(c.current,null,'sampling gap resets evidence');
c.reset();put('happy',0);uncertain(200);put('happy',400);c.accept(null,2,450);put('happy',600);
assert.equal(c.current,null,'multiple people clear previous votes');
c.reset();put('happy',0);put('happy',0);put('happy',0);put('happy',-1);
assert.equal(c.current,null,'duplicate or out of order input ignored');
c.reset();put('happy',0);put('happy',350);c.reset();assert.equal(c.current,null);
put('sad',400);assert.equal(c.current,null,'reset requires fresh confirmation');
console.log('PASS millisecond continuity, repeated majority, cooldown, noise, expiry, loss, duplicate input, reset');
