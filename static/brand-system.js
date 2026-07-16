(function(){
  "use strict";
  var WORDMARK="/mapdal-wordmark.svg";

  function normalized(el){
    return String(el&&el.textContent||"").replace(/\s+/g,"").toUpperCase();
  }

  function isBrand(el){
    var t=normalized(el);
    return t==="MAPDALSEOUL"||t==="맵달SEOUL";
  }

  function replace(el){
    if(!el||el.classList.contains("mapdal-wordmark")||!isBrand(el))return;
    el.classList.add("mapdal-wordmark");
    el.setAttribute("aria-label","맵달서울 홈");
    el.innerHTML='<img src="'+WORDMARK+'" alt="맵달SEOUL">';
  }

  function scan(root){
    var scope=root&&root.querySelectorAll?root:document;
    var nodes=scope.querySelectorAll(".logo, .site-logo, .brand-logo");
    for(var i=0;i<nodes.length;i++)replace(nodes[i]);
    /* The account sign-in card uses a heading instead of a .logo element. */
    var account=scope.querySelectorAll(".box>h1");
    for(var j=0;j<account.length;j++)if(isBrand(account[j])){
      account[j].classList.add("mp-account-brand");replace(account[j]);
    }
  }

  function start(){
    scan(document);
    try{
      new MutationObserver(function(ms){
        for(var i=0;i<ms.length;i++)for(var j=0;j<ms[i].addedNodes.length;j++){
          var n=ms[i].addedNodes[j];
          if(n.nodeType===1){
            if(n.matches&&n.matches(".logo,.site-logo,.brand-logo,.box>h1"))replace(n);
            scan(n);
          }
        }
      }).observe(document.body,{childList:true,subtree:true});
    }catch(e){}
  }

  if(document.readyState==="loading")document.addEventListener("DOMContentLoaded",start);
  else start();
})();
