// ============ 共享脚本：主题/设置初始化 ============
  // 初始化主题配置
  (function initSettings() {
	const NATURE_THEMES = [
	  { name: '靛蓝色', color: '#4f6ef7' },
	  { name: '桃红色', color: '#F56C6C' },
	  { name: '樱桃粉', color: '#FF69B4' },
	  { name: '荧光绿', color: '#32CD32' },
	  { name: '天空蓝', color: '#00BFFF' },
	  { name: '杨桃黄', color: '#DAA520' },
	  { name: '枫叶红', color: '#DC143C' }
	];
	const drawer = document.getElementById('settingsDrawer');
	const toggleBtn = document.getElementById('themeToggle');
	const colorBox = document.getElementById('themeColors');
	
	// ★ 深色遮罩层（与更新日志弹窗一致的毛玻璃特效）
	// 挂到抽屉所在的 .wrap 内，与抽屉处于同一堆叠上下文，避免遮罩盖住抽屉本身
	const backdrop = document.createElement('div');
	backdrop.className = 'settings-backdrop';
	(drawer.parentElement || document.body).appendChild(backdrop);
	
	document.getElementById('btnOpenSettings').onclick = () => { drawer.classList.add('open'); backdrop.classList.add('show'); };
	document.getElementById('btnCloseSettings').onclick = () => { drawer.classList.remove('open'); backdrop.classList.remove('show'); };

	// ★ 点击了外部区域，关闭弹窗
	document.addEventListener('click', (e) => {
	  // 如果抽屉没打开，直接跳过
	  if (!drawer.classList.contains('open')) return;
	  
	  // 判断点击位置：是否在抽屉内、是否点了打开按钮、是否点了右下角悬浮按钮
	  const isClickInside = drawer.contains(e.target);
	  const isClickOpenBtn = e.target.closest('#btnOpenSettings');
	  const isClickFloatBtn = e.target.closest('#floatSettings');
	  
	  // 如果都不在里面，就关闭抽屉
	  if (!isClickInside && !isClickOpenBtn && !isClickFloatBtn) {
		drawer.classList.remove('open');
		backdrop.classList.remove('show');
	  }
	});

	// 深色模式切换
	const savedMode = localStorage.getItem('themeMode');
	const prefersDark = window.matchMedia('(prefers-color-scheme: dark)').matches;
	
	function setThemeMode(isDark) {
	  document.documentElement.style.colorScheme = isDark ? 'dark' : 'light';
	  toggleBtn.checked = isDark;
	  localStorage.setItem('themeMode', isDark ? 'dark' : 'light');
	  
	  // ★ 高亮当前模式
	  const elLight = document.getElementById('themeLabelLight');
	  const elDark = document.getElementById('themeLabelDark');
	  if (elLight && elDark) {
		if (isDark) {
		  elLight.style.color = 'var(--muted)';
		  elLight.style.fontWeight = '600';
		  elDark.style.color = 'var(--primary-dark)';
		  elDark.style.fontWeight = '800';
		} else {
		  elLight.style.color = 'var(--primary-dark)';
		  elLight.style.fontWeight = '800';
		  elDark.style.color = 'var(--muted)';
		  elDark.style.fontWeight = '600';
		}
	  }
	}

	if (savedMode) {
	  setThemeMode(savedMode === 'dark');
	} else {
	  setThemeMode(prefersDark);
	}

	toggleBtn.onchange = (e) => setThemeMode(e.target.checked);

	// ===== 自定义主题色 DOM =====
	const customColorRow = document.getElementById('customColorRow');
	const customColorPicker = document.getElementById('customColorPicker');
	const customColorHex = document.getElementById('customColorHex');
	const customColorApplyBtn = document.getElementById('customColorApplyBtn');

	// ★ 规范化十六进制色值：#abc / #aabbcc / abc / aabbcc → #aabbcc
	function normalizeHex(str) {
	  if (!str) return null;
	  let s = String(str).trim().replace(/^#/, '');
	  if (/^[0-9a-fA-F]{3}$/.test(s)) s = s.split('').map(c => c + c).join('');
	  if (!/^[0-9a-fA-F]{6}$/.test(s)) return null;
	  return '#' + s.toLowerCase();
	}

	// ★ 核心：根据「单一基准色」动态计算所有主题色变体。
	// 浅色/深色两套变体分别用 CSS color-mix 生成，再交给 light-dark() 自动切换：
	//   --primary        主色（深色模式稍提亮，保证在深背景上清晰）
	//   --primary-dark   强调/文字/边框色（浅色模式压暗、深色模式提亮）
	//   --primary-light  浅淡底/高亮背景（浅色模式淡化为底、深色模式压暗为底）
	//   --primary-tint   悬浮发光用的半透明色调
	function applyThemeColor(baseHex) {
	  const root = document.documentElement.style;
	  root.setProperty('--primary', `light-dark(${baseHex}, color-mix(in srgb, ${baseHex} 82%, #fff))`);
	  root.setProperty('--primary-dark', `light-dark(color-mix(in srgb, ${baseHex} 60%, #000), color-mix(in srgb, ${baseHex} 70%, #fff))`);
	  root.setProperty('--primary-light', `light-dark(color-mix(in srgb, ${baseHex} 20%, #fff), color-mix(in srgb, ${baseHex} 28%, #000))`);
	  root.setProperty('--primary-tint', `color-mix(in srgb, ${baseHex} 35%, transparent)`);
	}

	// 同步自定义色输入框与当前颜色一致
	function syncCustomColorUI(hex) {
	  customColorPicker.value = hex;
	  customColorHex.value = hex;
	}

	// 高亮当前选中的预设按钮 / 自定义行
	function updateThemeSelection(hex) {
	  const matched = NATURE_THEMES.find(t => normalizeHex(t.color) === hex);
	  document.querySelectorAll('#themeColors .theme-btn').forEach(b => b.classList.remove('active'));
	  if (matched) {
		const btn = [...document.querySelectorAll('#themeColors .theme-btn')].find(b => b.dataset.color === matched.color);
		if (btn) btn.classList.add('active');
		customColorRow.classList.remove('active');
	  } else {
		customColorRow.classList.add('active');
	  }
	}

	// 仅应用外观（不写后端），供启动恢复 / 后端配置回填使用
	function renderThemeColor(baseHex) {
	  const base = normalizeHex(baseHex);
	  if (!base) return;
	  applyThemeColor(base);
	  syncCustomColorUI(base);
	  updateThemeSelection(base);
	  localStorage.setItem('themeColor', base);
	}

	// 用户主动选择：应用外观 + 持久化到 localStorage 与后端 global_config.json
	let themeSaveTimer = null;
	function selectThemeColor(baseHex) {
	  const base = normalizeHex(baseHex);
	  if (!base) return;
	  renderThemeColor(base);
	  clearTimeout(themeSaveTimer);
	  themeSaveTimer = setTimeout(() => {
		try {
		  fetch('http://localhost:8000/v1/global/config', {
			method: 'POST',
			headers: { 'Content-Type': 'application/json' },
			body: JSON.stringify({ theme_color: base })
		  }).catch(() => {});
		} catch (e) { console.warn('无法同步主题色到后端', e); }
	  }, 200);
	}

	// 暴露给后端配置加载：global_config.json 里的 theme_color 会覆盖 localStorage
	window.__applyThemeColor = renderThemeColor;

	// 主题色渲染 (适配 4行2列 Grid 布局)
	NATURE_THEMES.forEach((t, index) => {
	  const btn = document.createElement('button');
	  btn.className = 'theme-btn';
	  // 靛蓝色独占一行 (索引为0)
	  if (index === 0) btn.classList.add('full-width');

	  btn.dataset.color = t.color;
	  btn.innerHTML = `<span class="color-dot" style="background:${t.color}"></span> ${t.name}`;

	  btn.onclick = () => selectThemeColor(t.color);
	  colorBox.appendChild(btn);
	});

	// 恢复上次主题色：优先 localStorage（秒开，无闪烁），随后后端配置异步覆盖（权威）
	const savedColor = normalizeHex(localStorage.getItem('themeColor'));
	const legacyTheme = NATURE_THEMES.find(t => t.name === localStorage.getItem('themeColor'));
	renderThemeColor(savedColor || (legacyTheme ? legacyTheme.color : NATURE_THEMES[0].color));

	// 自定义颜色：颜色选择器（实时预览 + 持久化）
	customColorPicker.addEventListener('input', () => {
	  customColorHex.value = customColorPicker.value;
	  selectThemeColor(customColorPicker.value);
	});
	customColorPicker.addEventListener('change', () => {
	  customColorHex.value = customColorPicker.value;
	  selectThemeColor(customColorPicker.value);
	});

	// 十六进制输入：合法时实时应用，回车 / 点击“应用”确认
	function applyHexInput() {
	  const hex = normalizeHex(customColorHex.value);
	  if (hex) {
		customColorPicker.value = hex;
		customColorHex.value = hex;
		selectThemeColor(hex);
	  }
	}
	customColorHex.addEventListener('input', () => {
	  const hex = normalizeHex(customColorHex.value);
	  if (hex) {
		customColorPicker.value = hex;
		selectThemeColor(hex);
	  }
	});
	customColorHex.addEventListener('keydown', (e) => { if (e.key === 'Enter') applyHexInput(); });
	customColorApplyBtn.addEventListener('click', applyHexInput);

	// 渲染字体列表
	const FONTS = [
	  { name: '等宽编程风', value: 'ui-monospace, Consolas, "Courier New", monospace' },
	  { name: '明朝', value: '"TaishaMincho-DemiBold", "SimSun", "Songti SC", serif' },
	  { name: '楷体', value: '"FOT-TsukuMinPr6N-D", "KaiTi", "Kaiti SC", serif' },
	  { name: '圆体', value: '"TaishaMaru-DemiBold", "Nunito", "Varela Round", "Yuanti SC", "Hiragino Sans GB", sans-serif' },
	  { name: '黑体', value: '"TaishaGothic-DemiBold", "Microsoft YaHei", "PingFang SC", sans-serif' },
	];
	const fontList = document.getElementById('fontList');
	const savedFont = localStorage.getItem('themeFont') || FONTS[0].value;
	document.documentElement.style.setProperty('--font-family', savedFont);

	FONTS.forEach((f, index) => {
	  const btn = document.createElement('button');
	  btn.className = 'font-btn' + (savedFont === f.value ? ' active' : '');
	  // ★ 核心修改：让第一个（等宽编程风）独占一行
	  if (index === 0) btn.classList.add('full-width');
	  
	  btn.innerText = f.name;
	  btn.style.fontFamily = f.value;
	  btn.onclick = () => {
		document.documentElement.style.setProperty('--font-family', f.value);
		localStorage.setItem('themeFont', f.value);
		document.querySelectorAll('.font-btn').forEach(b => b.classList.remove('active'));
		btn.classList.add('active');
	  };
	  fontList.appendChild(btn);
	});

	// ===== TTS 输出形式配置 (适配按钮组) =====
	const formatBtns = document.querySelectorAll('#outputFormatGroup .btn');
	const sampleBox = document.getElementById('outputSampleBox');

	function updateSampleText(fmt) {
	  let outText = '';
	  if (fmt === 'A') outText = 'A > 漢字';
	  else if (fmt === 'B') outText = 'B > かんじ';
	  else if (fmt === 'C') outText = 'C > ｜漢字《かんじ》';
	  
	  sampleBox.textContent = `user_dict.txt\n漢字 => かんじ\n---\ninput: 漢字\n---\noutput:\n${outText}`;
	}

	const savedFmt = localStorage.getItem('outputFormat') || 'C';
	
	// 初始化按钮激活状态
	formatBtns.forEach(btn => {
	  if (btn.dataset.fmt === savedFmt) {
		btn.classList.remove('ghost'); // 移除 ghost 类以显示主色调
		btn.classList.add('active');
	  } else {
		btn.classList.add('ghost');
		btn.classList.remove('active');
	  }
	});
	updateSampleText(savedFmt);

	// 按钮点击事件
	formatBtns.forEach(btn => {
	  btn.addEventListener('click', async () => {
		const fmt = btn.dataset.fmt;
		
		// 更新 UI 状态
		formatBtns.forEach(b => {
		  b.classList.remove('active');
		  b.classList.add('ghost');
		});
		btn.classList.remove('ghost');
		btn.classList.add('active');
		
		localStorage.setItem('outputFormat', fmt);
		updateSampleText(fmt);
		
		try {
		  await fetch('http://localhost:8000/v1/global/config', {
			method: 'POST',
			headers: {'Content-Type': 'application/json'},
			body: JSON.stringify({ output_format: fmt })
		  });
		} catch(e) {
		  console.warn("无法同步输出格式到后端", e);
		}
	  });
	});
  })();

// ============ 共享脚本：代码块复制 ============
(function() {
  // 1. 动态注入 SVG 复制按钮
  function injectCopyButtons() {
    document.querySelectorAll('pre').forEach(pre => {
      if (pre.querySelector('.code-copy-btn')) return; // 避免重复注入
      
      const btn = document.createElement('button');
      btn.className = 'code-copy-btn';
      btn.type = 'button';
      btn.title = '复制代码';
      btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2" ry="2"></rect><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"></path></svg>`;
      
      pre.appendChild(btn);
    });
  }

  // 初始化注入
  document.addEventListener('DOMContentLoaded', injectCopyButtons);
  setTimeout(injectCopyButtons, 500); // 兜底，防止动态生成的代码块漏掉

  // 2. 点击事件处理
  document.addEventListener('click', async (e) => {
    const copyBtn = e.target.closest('.code-copy-btn');
    const targetEl = e.target.closest('pre') || e.target.closest('code');
    if (!targetEl) return;

    const textToCopy = targetEl.innerText.trim();
    if (!textToCopy) return;

    // 复制逻辑
    try {
      // 现代浏览器 API
      await navigator.clipboard.writeText(textToCopy);
      showCopyToast(e.clientX, e.clientY, '已复制!');
      animateBtnSuccess(copyBtn);
    } catch (err) {
      // 兼容老版本浏览器的回退方案
      try {
        const ta = document.createElement('textarea');
        ta.value = textToCopy;
        ta.style.position = 'fixed';
        ta.style.top = '-9999px';
        document.body.appendChild(ta);
        ta.select();
        document.execCommand('copy');
        document.body.removeChild(ta);
        showCopyToast(e.clientX, e.clientY, '已复制!');
        animateBtnSuccess(copyBtn);
      } catch (e2) {
        showCopyToast(e.clientX, e.clientY, '复制失败', true);
      }
    }
  });

  // 复制成功后的图标动画
  function animateBtnSuccess(btn) {
    if (!btn) return;
    const originalSvg = btn.innerHTML;
    btn.classList.add('copied');
    // 换成绿色的对勾
    btn.innerHTML = `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><polyline points="20 6 9 17 4 12"></polyline></svg>`;
    
    setTimeout(() => {
      btn.classList.remove('copied');
      btn.innerHTML = originalSvg;
    }, 2000);
  }

  // 显示提示框的函数
  function showCopyToast(x, y, msg, isError = false) {
    const toast = document.createElement('div');
    toast.className = 'copy-toast';
    toast.textContent = msg;
    
    if (isError) {
      toast.style.background = 'var(--danger)';
      toast.style.borderColor = 'color-mix(in srgb, var(--danger) 70%, #000)';
    }

    toast.style.left = x + 'px';
    toast.style.top = y + 'px';
    document.body.appendChild(toast);

    requestAnimationFrame(() => {
      toast.classList.add('show');
    });

    setTimeout(() => {
      toast.classList.remove('show');
      setTimeout(() => toast.remove(), 200);
    }, 1000);
  }
})();

// ============ 共享脚本：自定义下拉框 ============
(function initCustomDropdowns() {
  let activeSelect = null;
  let popupEl = null;

  // 关闭弹窗
  function closePopup() {
    if (popupEl) {
      popupEl.remove();
      popupEl = null;
    }
    activeSelect = null;
  }

  // 获取鼠标悬停位置所在的元素（用于拖拽）
  function getDragAfterElement(container, y) {
    const draggableElements = [...container.querySelectorAll('.custom-option:not(.dragging)')];
    return draggableElements.reduce((closest, child) => {
      const box = child.getBoundingClientRect();
      const offset = y - box.top - box.height / 2;
      if (offset < 0 && offset > closest.offset) {
        return { offset: offset, element: child };
      } else {
        return closest;
      }
    }, { offset: Number.NEGATIVE_INFINITY }).element;
  }

  // 创建选项节点
  function createOptionItem(opt, selectEl) {
    if (opt.hidden || opt.style.display === 'none') return null;

    const div = document.createElement('div');
    div.className = 'custom-option';
    if (opt.selected) div.classList.add('selected');
    if (opt.disabled) div.classList.add('disabled');
    div.dataset.value = opt.value;

    const canDrag = ['monitorCevioModel', 'monitorWindowsModel', 'monitorEdgeModel'].includes(selectEl.id);

    /* =========================================================
     * 非拖拽条目：保持原逻辑（点一下立即选中 + 关闭）
     * ========================================================= */
    if (!canDrag) {
      div.textContent = opt.textContent;
      div.addEventListener('mousedown', (e) => {
        if (popupEl && popupEl._draggedItem) return;
        e.preventDefault(); e.stopPropagation();
        if (opt.disabled) return;
        selectEl.value = opt.value;
        selectEl.dispatchEvent(new Event('change', { bubbles: true }));
        closePopup();
      });
      return div;
    }

    /* =========================================================
     * 可拖拽条目：左侧手柄 + 右侧文字
     * ========================================================= */
    div.style.display = 'flex';
    div.style.alignItems = 'center';
    div.style.gap = '6px';
    div.style.userSelect = 'none';

    // ---- 拖拽手柄（唯一可拖拽源）----
    const handle = document.createElement('span');
    handle.className = 'custom-drag-handle';
    handle.innerHTML = `<svg width="12" height="12" viewBox="0 0 24 24" fill="currentColor" style="pointer-events:none;display:block;"><circle cx="9" cy="6" r="1.6"/><circle cx="15" cy="6" r="1.6"/><circle cx="9" cy="12" r="1.6"/><circle cx="15" cy="12" r="1.6"/><circle cx="9" cy="18" r="1.6"/><circle cx="15" cy="18" r="1.6"/></svg>`;
    handle.style.cssText = 'display:inline-flex;align-items:center;justify-content:center;color:var(--muted);cursor:grab;flex-shrink:0;opacity:0.55;transition:opacity .15s,color .15s;';
    handle.draggable = true;
    handle.title = '按住拖动排序';
    div.appendChild(handle);

    // ---- 文字 ----
    const textSpan = document.createElement('span');
    textSpan.textContent = opt.textContent;
    textSpan.style.cssText = 'flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;';
    div.appendChild(textSpan);

    // ---- 长按 / 短按 / 拖拽 判定状态 ----
    let pressTime = 0, pressX = 0, pressY = 0;
    let isDragging = false, isLongPress = false, longPressTimer = null;

    const resetVisual = () => {
      div.style.outline = '';
      handle.style.color = 'var(--muted)';
      handle.style.opacity = '0.55';
      handle.style.cursor = 'grab';
    };

    div.addEventListener('mousedown', (e) => {
      if (opt.disabled) return;
      if (e.button !== 0) return;      // 只响应左键
      pressTime = Date.now();
      pressX = e.clientX; pressY = e.clientY;
      isDragging = false;
      isLongPress = false;

      clearTimeout(longPressTimer);
      longPressTimer = setTimeout(() => {
        // 长按满 500ms：标记为长按态，视觉反馈
        isLongPress = true;
        div.style.outline = '2px solid var(--primary)';
        handle.style.color = 'var(--primary)';
        handle.style.opacity = '1';
      }, 500);
    });

    div.addEventListener('mouseup', (e) => {
      clearTimeout(longPressTimer);
      if (opt.disabled) return;
      if (isDragging) { resetVisual(); return; }   // 拖拽已接管

      const elapsed = Date.now() - pressTime;
      const dx = Math.abs(e.clientX - pressX);
      const dy = Math.abs(e.clientY - pressY);
      resetVisual();

      // ★ 长按超过 500ms，或明显位移 → 不选中
      if (isLongPress || elapsed > 500 || dx > 5 || dy > 5) return;

      // ★ 短按（<500ms 且几乎无位移）→ 正常选中并关闭
      selectEl.value = opt.value;
      selectEl.dispatchEvent(new Event('change', { bubbles: true }));
      closePopup();
    });

    div.addEventListener('mouseleave', () => {
      clearTimeout(longPressTimer);
      if (!isDragging) resetVisual();
    });

    // 手柄悬停高亮
    handle.addEventListener('mouseenter', () => { if (!isDragging) handle.style.opacity = '1'; });
    handle.addEventListener('mouseleave', () => { if (!isDragging) handle.style.opacity = '0.55'; });

    // ---- 拖拽（必须挂在手柄上，因为 dragstart 不冒泡）----
    handle.addEventListener('dragstart', (e) => {
      isDragging = true;
      clearTimeout(longPressTimer);
      resetVisual();
      e.dataTransfer.effectAllowed = 'move';
      try { e.dataTransfer.setData('text/plain', div.dataset.value); } catch (_) {}
      // 延迟加 class，让浏览器先截图做拖拽图像，避免整行瞬间变灰
      setTimeout(() => div.classList.add('dragging'), 0);
      popupEl._draggedItem = div;
    });

    handle.addEventListener('dragend', () => {
      div.classList.remove('dragging');
      popupEl._draggedItem = null;
      isDragging = false;
      resetVisual();

      // 同步最新顺序到原生 select 并持久化
      const newOrder = Array.from(popupEl.querySelectorAll('.custom-option')).map(el => el.dataset.value);
      localStorage.setItem(`sort_${selectEl.id}`, JSON.stringify(newOrder));

      const options = Array.from(selectEl.options);
      const groups = Array.from(selectEl.querySelectorAll('optgroup'));
      if (groups.length > 0) {
        groups.forEach(g => {
          const groupVals = Array.from(g.options).map(o => o.value);
          newOrder.forEach(val => {
            if (groupVals.includes(val)) {
              const opt2 = options.find(o => o.value === val);
              if (opt2) g.appendChild(opt2);
            }
          });
        });
      } else {
        newOrder.forEach(val => {
          const opt2 = options.find(o => o.value === val);
          if (opt2) selectEl.appendChild(opt2);
        });
      }

      // ★ 异步持久化到后端（my_config.json / 当前激活配置）
      if (['monitorCevioModel', 'monitorWindowsModel', 'monitorEdgeModel'].includes(selectEl.id)) {
        fetch('http://localhost:8000/v1/models/config/order', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ order: { [selectEl.id]: newOrder } })
        }).then(r => {
          if (r.ok) {
            // 同步内存里的偏好，让 applyCustomSort 优先用最新顺序
            if (!window.__uiOrder) window.__uiOrder = {};
            window.__uiOrder[selectEl.id] = newOrder;
          } else {
            console.warn('持久化下拉排序失败，HTTP', r.status);
          }
        }).catch(e => console.warn('持久化下拉排序请求异常:', e));
      }

      if (typeof updateAutoConfigPreview === 'function') updateAutoConfigPreview();
    });

    return div;
  }

  // 生成弹窗
  function showPopup(selectEl) {
    closePopup();
    activeSelect = selectEl;

    popupEl = document.createElement('div');
    popupEl.className = 'custom-select-popup';

    const list = selectEl.children;
    for (let i = 0; i < list.length; i++) {
      const child = list[i];
      if (child.tagName === 'OPTGROUP') {
        const visibleItems = [];
        for (let j = 0; j < child.children.length; j++) {
          const item = createOptionItem(child.children[j], selectEl);
          if (item) visibleItems.push(item);
        }
        if (visibleItems.length > 0) {
          const groupLabel = document.createElement('div');
          groupLabel.className = 'custom-optgroup';
          groupLabel.textContent = child.label;
          popupEl.appendChild(groupLabel);
          visibleItems.forEach(el => popupEl.appendChild(el));
        }
      } else if (child.tagName === 'OPTION') {
        const item = createOptionItem(child, selectEl);
        if (item) popupEl.appendChild(item);
      }
    }

    // ★ 核心新增：在弹窗上监听 dragover 事件，实现实时位置交换
    popupEl.addEventListener('dragover', (e) => {
      e.preventDefault();
      if (!popupEl._draggedItem) return;
      
      const afterElement = getDragAfterElement(popupEl, e.clientY);
      if (afterElement == null) {
        popupEl.appendChild(popupEl._draggedItem);
      } else {
        popupEl.insertBefore(popupEl._draggedItem, afterElement);
      }
    });

    popupEl.style.visibility = 'hidden';
    document.body.appendChild(popupEl);
    
    const rect = selectEl.getBoundingClientRect();
    popupEl.style.left = rect.left + 'px';
    popupEl.style.width = Math.max(rect.width, 150) + 'px';
    
    const popupHeight = popupEl.offsetHeight;
    const spaceBelow = window.innerHeight - rect.bottom;
    const spaceAbove = rect.top;
    
    if (spaceBelow < popupHeight + 10 && spaceAbove > spaceBelow) {
      popupEl.style.maxHeight = Math.max(100, spaceAbove - 15) + 'px';
      popupEl.style.top = (rect.top - Math.min(popupHeight, spaceAbove - 10) - 4) + 'px';
    } else {
      popupEl.style.maxHeight = Math.max(100, spaceBelow - 15) + 'px';
      popupEl.style.top = (rect.bottom + 4) + 'px';
    }
    
    popupEl.style.visibility = 'visible';
  }

  document.addEventListener('mousedown', (e) => {
    const selectEl = e.target.closest('select');
    if (selectEl) {
      e.preventDefault(); 
      if (activeSelect === selectEl && popupEl) {
        closePopup();
      } else {
        showPopup(selectEl);
      }
    } else {
      if (popupEl && !popupEl.contains(e.target)) {
        closePopup();
      }
    }
  });

  window.addEventListener('scroll', (e) => {
    if (popupEl && popupEl.contains(e.target)) return;
    closePopup();
  }, { capture: true, passive: true });
  
  window.addEventListener('resize', closePopup, { passive: true });
})();

// ============ 共享脚本：背景图像 ============
(function initBackgroundImage() {
  const STORAGE_KEY = 'novel_tts_bg_config';
  const DEFAULT_CFG = { filename: '', opacity: 75, blur: 2, dim: 20, uiOpacity: 66, uiBlur: 6 };

  const filenameInput = document.getElementById('bgFilename');
  const extDisplay    = document.getElementById('bgExtDisplay');
  const applyBtn      = document.getElementById('bgApplyBtn');
  const clearBtn      = document.getElementById('bgClearBtn');
  const testBtn       = document.getElementById('bgTestBtn');
  const statusEl      = document.getElementById('bgStatus');
  const opacityInput  = document.getElementById('bgOpacity');
  const opacityVal    = document.getElementById('bgOpacityVal');
  const blurInput     = document.getElementById('bgBlur');
  const blurVal       = document.getElementById('bgBlurVal');
  const dimInput      = document.getElementById('bgDim');
  const dimVal        = document.getElementById('bgDimVal');
  
  const uiOpacityInput = document.getElementById('uiOpacity');
  const uiOpacityVal   = document.getElementById('uiOpacityVal');
  const uiBlurInput    = document.getElementById('uiBlur');
  const uiBlurVal      = document.getElementById('uiBlurVal');

  if (!filenameInput || !applyBtn || !extDisplay) return;

  // ★ 动画包装：把原本直接写在框里的文字，塞进一个子 span 里
  extDisplay.innerHTML = `<span class="bg-ext-text">${extDisplay.textContent || ''}</span>`;
  const extTextEl = extDisplay.querySelector('.bg-ext-text');

  const SUPPORTED_EXTS = ['.jpg', '.png', '.webp', '.gif', '.jpeg', '.bmp', '.svg'];
  let carouselTimer = null;
  let carouselIndex = 0;
  let flipTimeout = null; // 用于控制动画切换的 setTimeout

  // ★ 执行翻牌切换的动画函数
  function changeExtText(newExt) {
    if (!extTextEl || extTextEl.textContent === newExt) return;
    
    if (flipTimeout) clearTimeout(flipTimeout);
    
    // 1. 向上滑出并淡出
    extTextEl.classList.add('flip-out');
    
    // 2. 等 250ms（滑出动画时间）后，瞬间移动到底部并换字
    flipTimeout = setTimeout(() => {
      extTextEl.textContent = newExt;
      extTextEl.classList.remove('flip-out');
      extTextEl.classList.add('flip-prepare');
      
      void extTextEl.offsetWidth; // 强制浏览器重绘
      
      // 3. 向上滑入并淡入
      extTextEl.classList.remove('flip-prepare');
    }, 250);
  }

  function startCarousel() {
    if (carouselTimer) clearInterval(carouselTimer);
    if (!extTextEl.textContent) {
      changeExtText(SUPPORTED_EXTS[0]);
    }
    carouselTimer = setInterval(() => {
      carouselIndex++;
      changeExtText(SUPPORTED_EXTS[carouselIndex % SUPPORTED_EXTS.length]);
    }, 1500);
  }

  function stopCarousel(extText) {
    if (carouselTimer) {
      clearInterval(carouselTimer);
      carouselTimer = null;
    }
    // 如果当前正在动画中途，直接暴力终止并恢复文本
    if (flipTimeout) {
      clearTimeout(flipTimeout);
      flipTimeout = null;
      extTextEl.classList.remove('flip-out', 'flip-prepare');
      extTextEl.textContent = extText || '';
    } else {
      changeExtText(extText || '');
    }
  }

  let cfg = { ...DEFAULT_CFG };
  try {
    const saved = localStorage.getItem(STORAGE_KEY);
    if (saved) Object.assign(cfg, JSON.parse(saved));
  } catch (e) {}

  function saveCfg() {
    try { localStorage.setItem(STORAGE_KEY, JSON.stringify(cfg)); } catch (e) {}
  }

  function sanitizeFilename(raw) {
    let f = String(raw == null ? '' : raw).trim();
    if (!f) return null;
    f = f.replace(/\\/g, '/').replace(/^\.?\/?bg\//i, '');
    if (!f) return null;
    if (f.startsWith('/') || f.includes('../')) return null;
    if (/^[a-z][a-z0-9+.-]*:/i.test(f)) return null;
    if (f.includes('/')) return null;
    return f;
  }

  function splitFilename(fullName) {
    if (!fullName) return { base: '', ext: '' };
    const match = fullName.match(/^(.+?)(\.[a-zA-Z0-9]+)?$/);
    return match ? { base: match[1], ext: match[2] || '' } : { base: fullName, ext: '' };
  }

  function setStatus(msg, type) {
    if (!statusEl) return;
    statusEl.textContent = msg;
    statusEl.style.color = type === 'ok' ? 'var(--ok)'
                         : type === 'err' ? 'var(--danger)'
                         : 'var(--muted)';
  }

  function testImage(path) {
    return new Promise((resolve) => {
      const img = new Image();
      img.onload  = () => resolve({ ok: true, w: img.naturalWidth, h: img.naturalHeight });
      img.onerror = () => resolve({ ok: false });
      img.src = path;
    });
  }

  async function findValidImagePath(inputStr) {
    const extMatch = inputStr.match(/\.([a-zA-Z0-9]+)$/);
    let extensionsToTry = [...SUPPORTED_EXTS];
    
    if (extMatch) {
      const givenExt = '.' + extMatch[1].toLowerCase();
      if (!extensionsToTry.includes(givenExt)) {
        return { error: `不支持的图片格式：${givenExt}` };
      }
      extensionsToTry = [givenExt, ...extensionsToTry.filter(e => e !== givenExt)];
    }
    
    const pureBase = extMatch ? inputStr.slice(0, -extMatch[0].length) : inputStr;

    for (const ext of extensionsToTry) {
      const path = `../bg/${pureBase}${ext}`;
      const res = await testImage(path);
      if (res.ok) return { path, filename: pureBase + ext, ...res };
    }
    return { error: `未找到名为 "${inputStr}" 的图片（已尝试常见格式）` };
  }

  function applyStyleVars() {
    const root = document.documentElement;
    root.style.setProperty('--bg-opacity', String(cfg.opacity / 100));
    root.style.setProperty('--bg-blur',    cfg.blur + 'px');
    root.style.setProperty('--bg-dim',     String(cfg.dim / 100));
    root.style.setProperty('--ui-opacity', String(cfg.uiOpacity / 100));
    root.style.setProperty('--ui-blur',    cfg.uiBlur + 'px');
  }

  async function applyBackground() {
    let rawInput = filenameInput.value.trim();
    // 获取当前显示的后缀（直接从内层 span 拿）
    if (rawInput && extTextEl.textContent && !rawInput.includes('.')) {
      rawInput += extTextEl.textContent;
    }

    if (!rawInput) {
      cfg.filename = '';
      document.body.classList.remove('has-bg');
      document.documentElement.style.removeProperty('--bg-image');
      applyStyleVars();
      saveCfg();
      setStatus('背景图已清除', '');
      startCarousel();
      return;
    }

    const cleanName = sanitizeFilename(rawInput);
    if (!cleanName) {
      alert('❌ 文件名非法：不能包含路径、协议头或 .. 符号。');
      setStatus('❌ 文件名非法', 'err');
      return;
    }

    setStatus('🔍 正在智能检测文件…', '');
    const result = await findValidImagePath(cleanName);
    
    if (result.error) {
      alert(`❌ ${result.error}\n请确认图片已放入 bg 文件夹。`);
      setStatus(`❌ ${result.error}`, 'err');
      return;
    }

    cfg.filename = result.filename;
    document.documentElement.style.setProperty('--bg-image', `url("${result.path}")`);
    document.body.classList.add('has-bg');
    applyStyleVars();
    saveCfg();
    setStatus(`✅ 已应用：${result.path}（${result.w}×${result.h}）`, 'ok');
    
    const parts = splitFilename(result.filename);
    filenameInput.value = parts.base;
    stopCarousel(parts.ext);
  }

  /* ===== 初始化 UI ===== */
  if (cfg.filename) {
    const parts = splitFilename(cfg.filename);
    filenameInput.value = parts.base;
    stopCarousel(parts.ext);
    document.documentElement.style.setProperty('--bg-image', `url("../bg/${cfg.filename}")`);
    document.body.classList.add('has-bg');
    setStatus(`当前背景：bg/${cfg.filename}`, 'ok');
  } else {
    startCarousel();
  }
  
  opacityInput.value      = cfg.opacity;
  opacityVal.textContent  = cfg.opacity + '%';
  blurInput.value         = cfg.blur;
  blurVal.textContent     = cfg.blur + 'px';
  dimInput.value          = cfg.dim;
  dimVal.textContent      = cfg.dim + '%';
  
  uiOpacityInput.value    = cfg.uiOpacity;
  uiOpacityVal.textContent= cfg.uiOpacity + '%';
  uiBlurInput.value       = cfg.uiBlur;
  uiBlurVal.textContent   = cfg.uiBlur + 'px';

  applyStyleVars();

  /* ===== 事件 ===== */
  
  // ★ 核心修复：在这里直接绑定打开文件夹的事件，避免 onclick 丢失
  const btnOpenBg = document.getElementById('btnOpenBgFolder');
  if (btnOpenBg) {
    btnOpenBg.addEventListener('mouseenter', () => {
      btnOpenBg.style.background = 'var(--hover-bg)'; 
      btnOpenBg.style.color = 'var(--primary-dark)';
    });
    btnOpenBg.addEventListener('mouseleave', () => {
      btnOpenBg.style.background = 'var(--panel-bg)'; 
      btnOpenBg.style.color = 'var(--muted)';
    });
    btnOpenBg.addEventListener('click', async () => {
      try {
        const r = await fetch('http://localhost:8000/v1/rules/open', {
          method: 'POST',
          headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({ filename: 'bg' })
        });
        const data = await r.json();
        if (!r.ok) throw new Error(data.detail || '后端返回错误');
      } catch (e) {
        alert('打开失败：' + e.message);
      }
    });
  }

  applyBtn.addEventListener('click', applyBackground);
  
  filenameInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); applyBtn.click(); }
  });

  filenameInput.addEventListener('blur', () => {
    const val = filenameInput.value.trim();
    if (val.includes('.')) {
      const parts = splitFilename(val);
      filenameInput.value = parts.base;
      if (parts.ext) stopCarousel(parts.ext.toLowerCase());
    } else if (!val) {
      startCarousel();
    }
  });
  
  clearBtn.addEventListener('click', () => {
    filenameInput.value = '';
    cfg.filename = '';
    document.body.classList.remove('has-bg');
    document.documentElement.style.removeProperty('--bg-image');
    applyStyleVars();
    saveCfg();
    setStatus('背景图已清除', '');
    startCarousel();
  });
  
  testBtn.addEventListener('click', async () => {
    let rawInput = filenameInput.value.trim();
    if (!rawInput) { alert('请先输入文件名'); return; }
    
    if (extTextEl.textContent && !rawInput.includes('.')) {
      rawInput += extTextEl.textContent;
    }
    
    const cleanName = sanitizeFilename(rawInput);
    if (!cleanName) {
      alert('❌ 文件名非法：不能包含路径或 .. 符号。');
      setStatus('❌ 文件名非法', 'err');
      return;
    }

    setStatus('🔍 正在智能检测…', '');
    const result = await findValidImagePath(cleanName);
    if (result.error) {
      alert(`❌ ${result.error}`);
      setStatus(`❌ ${result.error}`, 'err');
    } else {
      setStatus(`✅ 找到图片：${result.path}（${result.w}×${result.h}）`, 'ok');
      const parts = splitFilename(result.filename);
      filenameInput.value = parts.base;
      stopCarousel(parts.ext);
    }
  });

  // 滑条事件
  opacityInput.addEventListener('input', () => {
    cfg.opacity = parseInt(opacityInput.value, 10);
    opacityVal.textContent = cfg.opacity + '%';
    document.documentElement.style.setProperty('--bg-opacity', String(cfg.opacity / 100));
    saveCfg();
  });
  blurInput.addEventListener('input', () => {
    cfg.blur = parseInt(blurInput.value, 10);
    blurVal.textContent = cfg.blur + 'px';
    document.documentElement.style.setProperty('--bg-blur', cfg.blur + 'px');
    saveCfg();
  });
  dimInput.addEventListener('input', () => {
    cfg.dim = parseInt(dimInput.value, 10);
    dimVal.textContent = cfg.dim + '%';
    document.documentElement.style.setProperty('--bg-dim', String(cfg.dim / 100));
    saveCfg();
  });
  uiOpacityInput.addEventListener('input', () => {
    cfg.uiOpacity = parseInt(uiOpacityInput.value, 10);
    uiOpacityVal.textContent = cfg.uiOpacity + '%';
    document.documentElement.style.setProperty('--ui-opacity', String(cfg.uiOpacity / 100));
    saveCfg();
  });
  uiBlurInput.addEventListener('input', () => {
    cfg.uiBlur = parseInt(uiBlurInput.value, 10);
    uiBlurVal.textContent = cfg.uiBlur + 'px';
    document.documentElement.style.setProperty('--ui-blur', cfg.uiBlur + 'px');
    saveCfg();
  });
})();

// ============ 共享脚本：更新日志悬浮窗口 ============
(function initChangelogModal() {
  const btn = document.getElementById('changelogBtn');
  if (!btn) return;
  btn.addEventListener('click', openChangelogModal);

  function openChangelogModal() {
    if (document.getElementById('changelogOverlay')) return;
    const overlay = document.createElement('div');
    overlay.id = 'changelogOverlay';
    overlay.style.cssText = 'position:fixed;inset:0;z-index:100000;background:rgba(0,0,0,0.5);display:flex;align-items:center;justify-content:center;padding:20px;backdrop-filter:blur(4px);-webkit-backdrop-filter:blur(4px);';
    overlay.innerHTML =
      '<div style="background:var(--card);width:100%;max-width:560px;max-height:84vh;border-radius:12px;border:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;box-shadow:0 10px 40px rgba(0,0,0,0.3);animation:modalPop 0.25s cubic-bezier(0.175,0.885,0.32,1.275);">' +
        '<div style="padding:14px 20px;border-bottom:1px solid var(--border);display:flex;justify-content:space-between;align-items:center;background:var(--panel-bg);">' +
          '<div style="font-size:16px;font-weight:800;color:var(--primary-dark);">📋 更新日志</div>' +
          '<button id="changelogOverlayClose" style="background:none;border:none;font-size:20px;cursor:pointer;color:var(--muted);line-height:1;">✖</button>' +
        '</div>' +
        '<iframe src="update_history.html" style="width:100%;height:70vh;border:0;display:block;background:var(--bg);"></iframe>' +
      '</div>';
    document.body.appendChild(overlay);
    overlay.addEventListener('click', (e) => { if (e.target === overlay) overlay.remove(); });
    overlay.querySelector('#changelogOverlayClose').addEventListener('click', () => overlay.remove());
  }
})();

// ============ 共享脚本：自动读取 update_history.html 并显示版本号 ============
(function initAppVersion() {
  const el = document.getElementById('appVersion');
  if (!el) return;

  function applyVersionFromHtml(html) {
    if (!html) return false;
    const m = html.match(/class="cl-title"[^>]*>\s*(v?\d+\.\d+\.\d+)/);
    if (m) { el.textContent = m[1]; return true; }
    return false;
  }

  // 方式一：走后端接口（file:// 下也能跨域读取；后端负责解析 update_history.html）
  fetch('http://localhost:8000/v1/version')
    .then(r => r.ok ? r.json() : Promise.reject(new Error('bad status')))
    .then(data => { if (data && data.version) el.textContent = data.version; })
    .catch(() => {
      // 方式二：直接抓取本地 update_history.html（在本地静态服务器如 http://localhost:8080 下可用）
      fetch('update_history.html')
        .then(r => r.ok ? r.text() : Promise.reject(new Error('bad status')))
        .then(html => { applyVersionFromHtml(html); })
        .catch(() => {});
    });
})();

// ============ 共享脚本：从 global_config 同步主题色 ============
(function syncGlobalTheme() {
  try {
    fetch('http://localhost:8000/v1/global/config').then(r => r.ok ? r.json() : null).then(cfg => {
      if (cfg && cfg.theme_color && window.__applyThemeColor) window.__applyThemeColor(cfg.theme_color);
    }).catch(() => {});
  } catch (e) {}
})();

// ============ 共享脚本：右下角悬浮工具（回顶/设置/回底） ============
(function () {
  const btnTop = document.getElementById('backToTop');
  const btnBottom = document.getElementById('backToBottom');
  const btnSet = document.getElementById('floatSettings');
  if (!btnTop || !btnSet || !btnBottom) return;
  
  const SHOW_AFTER = 400; // 滚动多少像素后才显示按钮
  let ticking = false;
  
  function update() {
    const scrollY = window.scrollY;
    const docHeight = document.documentElement.scrollHeight;
    const winHeight = window.innerHeight;
    
    // 判断是否到底部（误差 10px 以内算作底部）
    const isBottom = scrollY + winHeight >= docHeight - 10;
    
    // 统一控制是否整体显示
    if (scrollY > SHOW_AFTER) {
      btnSet.classList.add('show');
    } else {
      btnSet.classList.remove('show');
    }
    
    // 前往顶部：只要离开顶部区域就显示
    if (scrollY > SHOW_AFTER) {
      btnTop.classList.add('show');
    } else {
      btnTop.classList.remove('show');
    }
    
    // 前往底部：只要离开顶部区域，且还没到最底部，就显示
    if (scrollY > SHOW_AFTER && !isBottom) {
      btnBottom.classList.add('show');
    } else {
      btnBottom.classList.remove('show');
    }
    
    ticking = false;
  }
  
  window.addEventListener('scroll', () => {
    if (!ticking) { ticking = true; requestAnimationFrame(update); }
  }, { passive: true });
  
  // 前往顶部点击
  btnTop.addEventListener('click', () => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({ top: 0, behavior: reduce ? 'auto' : 'smooth' });
  });
  
  // 前往底部点击
  btnBottom.addEventListener('click', () => {
    const reduce = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
    window.scrollTo({ top: document.documentElement.scrollHeight, behavior: reduce ? 'auto' : 'smooth' });
  });
  
  // 打开设置抽屉
  btnSet.addEventListener('click', () => {
    const origSetBtn = document.getElementById('btnOpenSettings');
    if (origSetBtn) origSetBtn.click();
  });
  
  update();
})();
