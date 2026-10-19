/* One product owns its physical stock and customer purchase options. */
window.ProductInventory = (() => {
  const escape = value => String(value == null ? '' : value).replace(/[&<>"']/g,
    char => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[char]));
  const quantity = value => value == null ? '무제한' : Number(value).toLocaleString('ko-KR');
  const key = item => item.product_id || item.client_key;
  const nullable = value => value === '' ? null : Number(value);
  const field = (label, input) => `<label style="display:block;margin:8px 0;font-size:13px">${label}<br>${input}</label>`;
  const input = (name, value, type = 'text', extra = '') => `<input data-field="${name}" type="${type}" value="${escape(value)}" ${extra} style="width:100%;padding:8px;border:1px solid #ccc;font:inherit">`;

  async function request(url, body) {
    const response = await fetch(url, body ? {method:'POST', credentials:'same-origin',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify(body)} : {credentials:'same-origin'});
    const data = await response.json();
    if (!response.ok) throw new Error(typeof data.detail === 'string' ? data.detail : '저장하지 못했습니다. 입력값을 확인해 주세요.');
    return data;
  }

  function physicalCard(item, index) {
    return `<section data-kind="inventory_items" data-index="${index}" style="padding:12px;border:1px solid #ddd;margin:10px 0">
      ${field('재고명', input('name', item.name, 'text', 'maxlength="80"'))}
      ${field('기본 입고수량', input('baseline', item.baseline, 'number', 'min="0" step="1"'))}
      <div style="font-size:13px">현재 판매 가능 <b>${quantity(item.remaining)}</b> · 판매되어도 기본 입고수량은 유지됩니다.</div>
      ${item.product_id ? '' : '<button type="button" class="btn sm ghost" data-action="remove-item" style="margin-top:8px">입력 취소</button>'}
    </section>`;
  }

  function componentRows(option, inventory) {
    return (option.components || []).map((component, index) => `<div data-component="${index}" style="display:flex;gap:6px;margin:6px 0">
      <select data-component-field="product_id" aria-label="차감할 실물재고" style="flex:1;min-width:0;padding:8px">
        <option value="">재고 선택</option>${inventory.map(item => `<option value="${escape(key(item))}" ${key(item) === component.product_id ? 'selected' : ''}>${escape(item.name || '이름 없는 재고')}</option>`).join('')}
      </select><input data-component-field="qty" aria-label="차감 수량" type="number" min="1" max="99" step="1" value="${escape(component.qty)}" style="width:64px;padding:8px" ${option.mode === 'any' ? 'readonly' : ''}>
      <button type="button" class="btn sm ghost" data-action="remove-component">제외</button></div>`).join('');
  }

  function saleCard(option, index, inventory) {
    const standalone = option.mode === 'standalone';
    return `<section data-kind="sale_options" data-index="${index}" style="padding:12px;border:1px solid #ddd;margin:10px 0">
      ${field('판매 옵션명', input('name', option.name, 'text', 'maxlength="80"'))}
      ${field('판매가(원)', input('price', option.price, 'number', 'min="1" step="1"'))}
      ${field('재고 차감 방식', `<select data-field="mode" style="width:100%;padding:8px;font:inherit">
        ${[['any','랜덤 — 선택한 재고 중 1장'], ['all','세트 — 구성별 수량 차감'], ['standalone','단독 — 이 옵션의 재고 사용']].map(([mode, label]) => `<option value="${mode}" ${option.mode === mode ? 'selected' : ''}>${label}</option>`).join('')}</select>`)}
      ${standalone ? field('기본 입고수량 · 비우면 무제한', input('baseline', option.baseline, 'number', 'min="0" step="1"')) + `<div class="hint">현재 판매 가능 ${quantity(option.remaining)}</div>` :
        componentRows(option, inventory) + '<button type="button" class="btn sm ghost" data-action="add-component">+ 차감 재고 선택</button>'}
      <div class="hint">${standalone ? '다른 옵션과 재고를 공유하지 않습니다.' : option.mode === 'any' ? '주문 수량만큼 재고가 많은 버전부터 차감합니다.' : '한 세트가 팔리면 위 구성 수량이 각각 차감됩니다.'}</div>
      ${field('고객에게 표시할 구성 안내 · 한 줄에 한 항목', `<textarea data-field="items" rows="2" style="width:100%;padding:8px;border:1px solid #ccc;font:inherit">${escape((option.items || []).map(item => item.t).join('\n'))}</textarea>`)}
      <button type="button" class="btn sm ghost" data-action="remove-sale" style="margin-top:12px;color:#b42318">${option.product_id ? '이 옵션 판매 종료' : '입력 취소'}</button>
    </section>`;
  }

  function mount(container, productId) {
    let state = null;
    let busy = false;
    let dirty = false;
    let message = '';
    const markDirty = () => { dirty = true; message = ''; };
    const update = (kind, index, transform) => {
      state = {...state, [kind]:state[kind].map((item, i) => i === index ? transform(item) : item)};
      markDirty();
    };

    function render() {
      if (!state) {
        container.innerHTML = `<p role="status">${escape(message || '재고를 불러오는 중입니다…')}</p><button type="button" class="btn sm ghost" data-action="reload">다시 불러오기</button>`;
        return;
      }
      const plain = !state.inventory_items.length && !state.sale_options.length;
      container.innerHTML = `<fieldset ${busy ? 'disabled' : ''} style="border:0;padding:0;min-width:0">
        <p class="hint">기본 입고수량에서 주문으로 확보된 수량을 차감해 판매 가능 수량을 계산합니다. 랜덤과 세트는 아래 실물재고를 함께 사용합니다.</p>
        ${plain ? `<section data-kind="parent_stock" style="margin:12px 0">${field('상품 기본 입고수량 · 비우면 무제한', input('baseline', state.parent_stock.baseline, 'number', 'min="0" step="1"'))}<div>현재 판매 가능 <b>${quantity(state.parent_stock.remaining)}</b></div></section>` : ''}
        <h4 style="margin-top:18px">버전별 실물재고</h4>
        ${state.inventory_items.map(physicalCard).join('') || '<p class="hint">공유할 버전별 재고가 없으면 단독 옵션으로 판매할 수 있습니다.</p>'}
        <button type="button" class="btn sm ghost" data-action="add-item">+ 실물재고 추가</button>
        <h4 style="margin-top:22px">고객 구매 옵션</h4>
        ${state.sale_options.map((option, index) => saleCard(option, index, state.inventory_items)).join('')}
        <button type="button" class="btn sm ghost" data-action="add-sale">+ 판매 옵션 추가</button>
        <div style="display:flex;gap:8px;margin-top:20px;flex-wrap:wrap"><button type="button" class="btn" data-action="save">${busy ? '저장 중…' : '재고·판매 옵션 저장'}</button>
          <button type="button" class="btn sm ghost" data-action="reload">최신 수량 불러오기</button></div>
        <p class="hint">상품명·이미지·설명은 페이지 아래의 저장 버튼으로 별도 저장합니다.</p>
        <p role="status" aria-live="polite" style="margin-top:10px;font-size:13px">${escape(message)}</p>
      </fieldset>`;
    }

    async function load() {
      if (busy || (dirty && !confirm('입력 중인 재고·옵션 변경을 버리고 최신 내용을 불러올까요?'))) return;
      busy = true;
      try {
        state = await request('/admin/api/products/inventory?id=' + encodeURIComponent(productId));
        dirty = false;
        message = '현재 재고를 불러왔습니다.';
      } catch (error) { message = error.message; }
      finally { busy = false; render(); }
    }

    async function save() {
      if (busy || !state) return;
      if (state.inventory_items.some(item => !item.name.trim() || !Number.isInteger(item.baseline) || item.baseline < 0)) {
        message = '각 실물재고의 이름과 0 이상의 정수 입고수량을 입력하세요.'; render(); return;
      }
      if (state.sale_options.some(option => !option.name.trim() || !Number.isInteger(option.price) || option.price <= 0 ||
        (option.mode !== 'standalone' && !(option.components || []).length))) {
        message = '판매 옵션명, 판매가, 차감할 재고를 확인하세요.'; render(); return;
      }
      busy = true;
      render();
      try {
        state = await request('/admin/api/products/inventory/save', state);
        dirty = false;
        message = '재고와 판매 옵션을 저장했습니다. 현재 주문 내역을 반영한 수량입니다.';
      } catch (error) {
        message = error.message + ' 입력 내용은 유지되었습니다. 최신 내용을 불러온 뒤 다시 확인해 주세요.';
      } finally { busy = false; render(); }
    }

    container.addEventListener('input', event => {
      if (!state || busy) return;
      const section = event.target.closest('[data-kind]');
      if (!section) return;
      const {kind, index} = section.dataset;
      const name = event.target.dataset.field;
      if (kind === 'parent_stock' && name) {
        state = {...state, parent_stock:{...state.parent_stock, [name]:nullable(event.target.value)}};
        markDirty(); return;
      }
      const componentField = event.target.dataset.componentField;
      if (componentField) {
        const componentIndex = Number(event.target.closest('[data-component]').dataset.component);
        update(kind, Number(index), option => ({...option, components:option.components.map((component, i) =>
          i === componentIndex ? {...component, [componentField]:componentField === 'qty' ? Number(event.target.value) : event.target.value} : component)}));
      } else if (name) {
        update(kind, Number(index), item => ({...item, [name]:name === 'items' ? event.target.value.split('\n').filter(line => line.trim()).map((line, i) => ({k:(item.items[i] || {}).k || 'etc', t:line})) :
          ['baseline','price'].includes(name) ? nullable(event.target.value) : event.target.value}));
      }
    });

    container.addEventListener('change', event => {
      if (event.target.dataset.field === 'mode') {
        const section = event.target.closest('[data-kind]');
        const index = Number(section.dataset.index);
        update('sale_options', index, option => ({...option, components:option.mode === 'any' ? (option.components || []).map(component => ({...component, qty:1})) : option.components}));
        render();
      }
    });

    container.addEventListener('click', event => {
      const button = event.target.closest('[data-action]');
      if (!button || busy) return;
      const action = button.dataset.action;
      if (action === 'reload') { load(); return; }
      if (action === 'save') { save(); return; }
      if (!state) return;
      const section = button.closest('[data-kind]');
      const index = section ? Number(section.dataset.index) : -1;
      if (action === 'add-item') state = {...state, inventory_items:[...state.inventory_items,
        {client_key:'new-' + crypto.randomUUID(), product_id:'', name:'', baseline:0, expected_baseline:null, remaining:0}]};
      if (action === 'add-sale') state = {...state, sale_options:[...state.sale_options,
        {product_id:'', name:'', price:0, mode:state.inventory_items.length ? 'any' : 'standalone', components:[], items:[], baseline:null, expected_baseline:null, remaining:null}]};
      if (action === 'remove-item') {
        const removedKey = key(state.inventory_items[index]);
        state = {...state, inventory_items:state.inventory_items.filter((_, i) => i !== index),
          sale_options:state.sale_options.map(option => ({...option, components:(option.components || []).filter(component => component.product_id !== removedKey)}))};
      }
      if (action === 'remove-sale') {
        if (state.sale_options[index].product_id && !confirm('이 옵션의 신규 판매를 종료할까요? 기존 주문과 재고는 보존됩니다.')) return;
        state = {...state, sale_options:state.sale_options.filter((_, i) => i !== index)};
      }
      if (action === 'add-component') update('sale_options', index, option => ({...option, components:[...(option.components || []), {product_id:'', qty:1}]}));
      if (action === 'remove-component') {
        const componentIndex = Number(button.closest('[data-component]').dataset.component);
        update('sale_options', index, option => ({...option, components:option.components.filter((_, i) => i !== componentIndex)}));
      }
      markDirty(); render();
    });
    window.addEventListener('beforeunload', event => { if (dirty) { event.preventDefault(); event.returnValue = ''; } });
    render(); load();
  }
  return {mount};
})();
