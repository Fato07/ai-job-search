export default async function run(page) {
  const dashboard = page.url().split(/[?#]/)[0];
  const checks = {};
  const check = (name, condition, detail = null) => {
    if (!condition) throw new Error(`${name} failed${detail ? `: ${detail}` : ''}`);
    checks[name] = detail ?? true;
  };
  const numericTableTotal = async (selector) => {
    const values = await page.locator(`${selector} tbody td:last-child`).allTextContents();
    return values.reduce((total, value) => total + Number.parseInt(value.replace(/\D/g, ''), 10), 0);
  };

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.waitForSelector('.pipeline-table tbody tr');
  check('total application count', (await page.locator('#pipeline-count').innerText()).includes('99'));
  check('five accessible chart families', await page.locator('svg[role="img"]').count() === 5, String(await page.locator('svg[role="img"]').count()));
  check('five chart text equivalents', await page.locator('.chart-table').count() === 5, String(await page.locator('.chart-table').count()));
  check('all chart titles and descriptions', await page.locator('svg[role="img"] > title').count() === 5 && await page.locator('svg[role="img"] > desc').count() === 5);
  check('daily cadence default', (await page.locator('#time-series-chart svg > title').textContent()).includes('Daily'));
  check('explicit axes', (await page.locator('#time-series-chart').innerText()).includes('Daily Period Start') && (await page.locator('#time-series-chart').innerText()).includes('Submission Count'));
  check('sortable table semantics', await page.locator('th[aria-sort]').count() >= 5);
  check('desktop filters expanded', await page.locator('#filter-disclosure').getAttribute('open') !== null);
  for (const [selector, axisTitle] of [
    ['#funnel-chart', 'Count'],
    ['#calibration-chart', 'Progression'],
    ['#feedback-category-chart', 'Count'],
    ['#aging-chart', 'Count'],
  ]) {
    check(`${selector} numeric axis`, await page.locator(`${selector} svg .axis-line-strong`).count() === 1);
    check(`${selector} axis title`, (await page.locator(`${selector} svg`).textContent()).includes(axisTitle));
  }
  const submittedFunnelRow = page.locator('#funnel-chart .chart-table tbody tr').filter({ hasText: 'Submitted' });
  check('global cumulative funnel submitted count', await submittedFunnelRow.locator('td').innerText() === '72', await submittedFunnelRow.innerText());
  const calibration90 = page.locator('#calibration-chart .chart-table tbody tr').filter({ hasText: '90-100' });
  const calibration90Text = await calibration90.innerText();
  check('global event calibration matches snapshot', calibration90Text.includes('19 submitted') && calibration90Text.includes('5 responded') && calibration90Text.includes('0 interviewed') && calibration90Text.includes('0 offered'), calibration90Text);
  const dailyRows = page.locator('#time-series-chart .chart-table tbody tr');
  check('daily cadence full continuous range', await dailyRows.count() === 55, String(await dailyRows.count()));
  check('daily cadence starts at snapshot start', (await dailyRows.first().innerText()).includes('Jul 1, 2026'));
  check('daily cadence ends at snapshot date', (await dailyRows.last().innerText()).includes('Aug 24, 2026'));
  check('daily cadence preserves zero days', await page.locator('#time-series-chart .chart-table tbody td:last-child').filter({ hasText: /^0$/ }).count() > 0);
  check('daily cadence total matches cumulative submitted', await numericTableTotal('#time-series-chart .chart-table') === 72);

  await page.selectOption('#cadence-granularity', 'weekly');
  await page.waitForFunction(() => document.querySelector('#time-series-chart svg title')?.textContent.includes('Weekly'));
  check('weekly cadence renders', (await page.locator('#time-series-chart svg > title').textContent()).includes('Weekly'));
  check('weekly cadence text table', (await page.locator('#time-series-chart .chart-table caption').innerText()).includes('Weekly'));
  const weeklyRows = page.locator('#time-series-chart .chart-table tbody tr');
  check('weekly cadence full continuous range', await weeklyRows.count() === 9, String(await weeklyRows.count()));
  check('weekly cadence total matches cumulative submitted', await numericTableTotal('#time-series-chart .chart-table') === 72);
  check('cadence URL state', page.url().includes('cadence=weekly'), page.url());
  await page.locator('#reset-filters').click();

  await page.goto(dashboard);
  await page.waitForSelector('.pipeline-table tbody tr');
  await page.locator('body').focus();
  await page.keyboard.press('Tab');
  check('skip link first', await page.evaluate(() => document.activeElement?.classList.contains('skip-link')));
  await page.keyboard.press('Enter');
  check('skip link targets main', await page.evaluate(() => document.activeElement?.id === 'main'));
  await page.locator('.skip-link').focus();
  const focusOrder = [];
  for (let step = 0; step < 8; step += 1) {
    await page.keyboard.press('Tab');
    focusOrder.push(await page.evaluate(() => document.activeElement?.textContent?.trim() || document.activeElement?.id || document.activeElement?.tagName));
  }
  check('keyboard traverses complete nav', ['Today', 'Funnel', 'Calibration', 'Feedback', 'Pipeline', 'Data Quality'].every((value) => focusOrder.includes(value)), JSON.stringify(focusOrder));

  await page.selectOption('#role-family', 'applied_ai');
  await page.selectOption('#stage', 'submitted');
  await page.waitForFunction(() => document.querySelector('#pipeline-count')?.textContent !== '99 visible applications');
  const combinedCount = Number.parseInt((await page.locator('#pipeline-count').innerText()).replace(/\D/g, ''), 10);
  check('combined filters update all-view count', combinedCount === 16, String(combinedCount));
  check('combined filters persist in URL', page.url().includes('role_family=applied_ai') && page.url().includes('stage=submitted'), page.url());
  check('today submission semantics remain event-based', (await page.locator('.throughput-band').nth(1).locator('.band-value').innerText()) === '0 / 20', await page.locator('.throughput-band').nth(1).locator('.band-value').innerText());
  check('filtered chart families all update', await page.evaluate(() => [...document.querySelectorAll('.chart-shell')].length === 5 && [...document.querySelectorAll('.chart-shell')].every((chart) => chart.childElementCount > 0)));
  const filteredSubmittedRow = page.locator('#funnel-chart .chart-table tbody tr').filter({ hasText: 'Submitted' });
  check('filtered cumulative funnel uses lifecycle IDs', await filteredSubmittedRow.locator('td').innerText() === '16', await filteredSubmittedRow.innerText());
  const filteredCalibrationText = (await page.locator('#calibration-chart .chart-table tbody tr').allTextContents()).join(' ');
  check('filtered calibration uses lifecycle counts', filteredCalibrationText.includes('submitted') && !filteredCalibrationText.includes('Not recorded'), filteredCalibrationText.slice(0, 300));
  await page.locator('#reset-filters').click();

  await page.selectOption('#feedback-category', 'metric_rigor_provenance');
  await page.waitForFunction(() => document.querySelector('#pipeline-count')?.textContent.startsWith('1 '));
  check('exact category filtering', (await page.locator('#pipeline-count').innerText()).startsWith('1 '));
  check('category URL state', page.url().includes('feedback_category=metric_rigor_provenance'), page.url());
  check('category chart isolated', await page.locator('#feedback-category-chart .chart-table tbody tr').count() === 1);
  check('lineage category isolated', await page.locator('details[data-rule-id]').count() === 1);
  const lineage = page.locator('details[data-rule-id]').first();
  await lineage.locator('summary').click();
  await page.waitForFunction(() => location.search.includes('feedback='));
  const lineageText = await lineage.innerText();
  check('real lineage excerpt', lineageText.includes('headline extraction metric lacked a defensible denominator'), lineageText.slice(0, 300));
  check('real lineage action', lineageText.includes('Every headline metric includes denominator'), lineageText.slice(0, 300));
  check('real lineage confidence', lineageText.includes('95%'), lineageText.slice(0, 300));
  check('feedback source category visible', lineageText.includes('Category: Metric Rigor Provenance'), lineageText.slice(-400));
  check('feedback source application ID visible', lineageText.includes('app-20260721-nordea-senior-data-scientist-ai-engineer-generative-ai-1878-16b2ad0e'), lineageText.slice(-500));
  check('lineage bounded to visible IDs', await lineage.locator('[data-application-id]').count() === 1);
  await lineage.locator('[data-application-id]').click();
  await page.waitForFunction(() => document.activeElement?.matches('tr[id^="row-"]'));
  check('feedback application action searches pipeline', (await page.locator('#pipeline-search').inputValue()).startsWith('app-'));
  check('feedback application action focuses row', await page.evaluate(() => document.activeElement?.matches('tr[id^="row-"]')));
  await page.locator('#reset-filters').click();

  const staleQuality = page.locator('#quality-list .quality-item').filter({ hasText: 'Stale Applications' });
  check('all stale targets rendered', await staleQuality.locator('[data-application-id]').count() === 58, String(await staleQuality.locator('[data-application-id]').count()));
  check('all stale targets disclosed by count', (await staleQuality.locator('.quality-targets > summary').innerText()).includes('58'));
  await page.evaluate(() => document.querySelectorAll('#quality-list .quality-targets').forEach((details) => { details.open = true; }));
  const qualityButtons = page.locator('#quality-list [data-application-id]');
  check('actionable quality applications', await qualityButtons.count() >= 58, String(await qualityButtons.count()));
  let offPageIndex = -1;
  for (let index = 0; index < await qualityButtons.count(); index += 1) {
    const id = await qualityButtons.nth(index).getAttribute('data-application-id');
    const rendered = await page.locator(`#row-${id}`).count();
    if (!rendered) { offPageIndex = index; break; }
  }
  check('off-page quality target available', offPageIndex >= 0, String(offPageIndex));
  const offPageId = await qualityButtons.nth(offPageIndex).getAttribute('data-application-id');
  await qualityButtons.nth(offPageIndex).click();
  await page.waitForFunction(() => document.activeElement?.matches('tr[id^=\"row-\"]'));
  check('off-page quality action searches exact ID', await page.locator('#pipeline-search').inputValue() === offPageId, await page.locator('#pipeline-search').inputValue());
  check('off-page quality action renders row', await page.locator(`#row-${offPageId}`).count() === 1);
  check('off-page quality action focuses row', await page.evaluate(() => document.activeElement?.id) === `row-${offPageId}`);
  check('snapshot-wide quality scope labeled', (await page.locator('#quality-list').innerText()).includes('Snapshot-wide, non-row scope'));
  await page.locator('#reset-filters').click();
  await page.evaluate(() => document.querySelectorAll('#quality-list .quality-targets').forEach((details) => { details.open = true; }));
  const globalFocusButton = page.locator('#quality-list [data-focus-target]').first();
  const globalTargetId = await globalFocusButton.getAttribute('data-focus-target');
  await globalFocusButton.click();
  check('global quality action focuses rendered item', await page.evaluate(() => document.activeElement?.id) === globalTargetId, await page.evaluate(() => document.activeElement?.id));

  const firstBefore = await page.locator('.company-cell strong').first().innerText();
  await page.locator('[data-sort="company"]').click();
  const companyHeader = page.locator('th').filter({ has: page.locator('[data-sort="company"]') });
  check('sort announces direction', ['ascending', 'descending'].includes(await companyHeader.getAttribute('aria-sort')));
  const firstAfter = await page.locator('.company-cell strong').first().innerText();
  check('sort changes row order', firstAfter !== firstBefore, `${firstBefore} → ${firstAfter}`);
  await page.locator('#pipeline-search').fill('ESTO');
  await page.waitForFunction(() => !document.querySelector('#pipeline-count')?.textContent.startsWith('99 '));
  const searchCount = Number.parseInt((await page.locator('#pipeline-count').innerText()).replace(/\D/g, ''), 10);
  check('search narrows results', searchCount === 2, String(searchCount));
  check('search persists in URL', page.url().includes('search=ESTO'), page.url());
  await page.locator('#pipeline-search').fill('');
  await page.waitForFunction(() => document.querySelector('#pipeline-count')?.textContent.startsWith('99 '));
  await page.locator('[data-page="next"]').click();
  check('pagination advances', (await page.locator('#pipeline-pagination').innerText()).includes('Page 2 of 2'));
  check('pagination persists in URL', page.url().includes('page=2'), page.url());
  await page.locator('#reset-filters').click();

  await page.locator('#date-start').fill('2026-08-24');
  await page.locator('#date-start').dispatchEvent('change');
  await page.locator('#date-end').fill('2026-07-01');
  await page.locator('#date-end').dispatchEvent('change');
  await page.waitForFunction(() => document.querySelector('#pipeline-count')?.textContent.startsWith('0 '));
  check('no-data states appear', await page.locator('.empty-state').count() >= 6, String(await page.locator('.empty-state').count()));
  check('empty states have next action', await page.locator('.empty-state [data-empty-action="reset"]').count() >= 6);
  await page.locator('#reset-filters').click();

  await page.goto(`${dashboard}?role_family=applied_ai&stage=closed&tab=pipeline&cadence=weekly`);
  await page.waitForSelector('.pipeline-table');
  check('URL restores role filter', await page.locator('#role-family').inputValue() === 'applied_ai');
  check('URL restores stage filter', await page.locator('#stage').inputValue() === 'closed');
  check('URL restores tab', await page.locator('[data-tab="pipeline"]').getAttribute('aria-current') === 'location');
  check('URL restores cadence', await page.locator('#cadence-granularity').inputValue() === 'weekly');
  await page.locator('#reset-filters').click();

  await page.evaluate(() => window.scrollTo(0, 0));
  await page.screenshot({ path: '/tmp/task9-dashboard-desktop.png', fullPage: false });
  const desktopLayout = await page.evaluate(() => ({
    width: innerWidth,
    bodyOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
    externalResources: [...document.querySelectorAll('script[src],link[rel="stylesheet"],img[src]')].length
  }));
  check('desktop width', desktopLayout.width === 1440, JSON.stringify(desktopLayout));
  check('no external assets', desktopLayout.externalResources === 0);

  await page.setViewportSize({ width: 390, height: 844 });
  await page.waitForFunction(() => innerWidth === 390);
  const mobileLayout = await page.evaluate(() => {
    const navLinks = [...document.querySelectorAll('.section-nav a')].map((link) => {
      const rect = link.getBoundingClientRect();
      return { text: link.textContent.trim(), left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom };
    });
    const controls = [...document.querySelectorAll('button,input,select,a,summary')];
    const shortTargets = controls.filter((element) => {
      const rect = element.getBoundingClientRect();
      return rect.width > 0 && rect.height > 0 && rect.height < 44;
    }).map((element) => `${element.tagName}:${element.textContent || element.getAttribute('aria-label') || element.id}:${element.getBoundingClientRect().height}`);
    return {
      width: innerWidth,
      bodyOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      shortTargets: shortTargets.slice(0, 8),
      filterOpen: document.querySelector('#filter-disclosure').open,
      filterSummary: document.querySelector('#filter-disclosure summary').innerText,
      navLinks,
      navRows: [...new Set(navLinks.map((link) => Math.round(link.top)))].length,
      commandTop: document.querySelector('#command-title').getBoundingClientRect().top
    };
  });
  check('mobile width', mobileLayout.width === 390, JSON.stringify(mobileLayout));
  check('mobile body has no horizontal overflow', mobileLayout.bodyOverflow === 0, JSON.stringify(mobileLayout));
  check('mobile filters collapsed by default', mobileLayout.filterOpen === false, JSON.stringify(mobileLayout));
  check('mobile filter summary names active count', mobileLayout.filterSummary.includes('Filters') && mobileLayout.filterSummary.includes('0 active'), mobileLayout.filterSummary);
  check('mobile nav complete and two-row', mobileLayout.navLinks.length === 6 && mobileLayout.navRows === 2 && mobileLayout.navLinks.every((link) => link.left >= 0 && link.right <= 390), JSON.stringify(mobileLayout.navLinks));
  check('analytics visible in first mobile viewport', mobileLayout.commandTop > 0 && mobileLayout.commandTop < 844, String(mobileLayout.commandTop));
  check('44px touch targets', mobileLayout.shortTargets.length === 0, JSON.stringify(mobileLayout.shortTargets));
  await page.screenshot({ path: '/tmp/task9-dashboard-mobile.png', fullPage: false });
  await page.locator('#filter-disclosure summary').click();
  check('mobile filter disclosure opens', await page.locator('#filter-disclosure').getAttribute('open') !== null);
  await page.locator('.skip-link').focus();
  await page.keyboard.press('Tab');
  const mobileNextFocus = await page.evaluate(() => ({ tag: document.activeElement?.tagName, text: document.activeElement?.textContent?.trim() }));
  check('mobile keyboard traverses navigation', mobileNextFocus.tag === 'A' && mobileNextFocus.text === 'Today', JSON.stringify(mobileNextFocus));

  await page.setViewportSize({ width: 1440, height: 1000 });
  await page.goto('file:///tmp/task9-dashboard-review-fixture.html');
  await page.waitForSelector('#quality-list');
  const linkedReview = page.locator('#quality-list .quality-item').filter({ hasText: 'Review Queue, Linked Applications' });
  const unmappedReview = page.locator('#quality-list .quality-item').filter({ hasText: 'Review Queue, Unmapped Items' });
  check('mixed review branches both render', await linkedReview.count() === 1 && await unmappedReview.count() === 1);
  await linkedReview.locator('.quality-targets > summary').click();
  check('linked review application reachable', await linkedReview.locator('[data-application-id=\"app-browser-linked\"]').count() === 1);
  await linkedReview.locator('[data-application-id=\"app-browser-linked\"]').click();
  await page.waitForFunction(() => document.activeElement?.id === 'row-app-browser-linked');
  check('linked review action focuses pipeline row', await page.locator('#pipeline-search').inputValue() === 'app-browser-linked');
  const refreshedUnmappedReview = page.locator('#quality-list .quality-item').filter({ hasText: 'Review Queue, Unmapped Items' });
  await refreshedUnmappedReview.locator('.quality-targets > summary').click();
  check('unmapped review details render', (await refreshedUnmappedReview.innerText()).includes('review-browser-global') && (await refreshedUnmappedReview.innerText()).includes('Resolve this review without an application ID.'));
  const reviewFocusButton = refreshedUnmappedReview.locator('[data-focus-target]').first();
  const reviewTargetId = await reviewFocusButton.getAttribute('data-focus-target');
  await reviewFocusButton.click();
  check('unmapped review action focuses rendered review item', await page.evaluate(() => document.activeElement?.id) === reviewTargetId);

  return {
    checks,
    screenshots: ['/tmp/task9-dashboard-desktop.png', '/tmp/task9-dashboard-mobile.png'],
    desktopLayout,
    mobileLayout,
    finalUrl: page.url()
  };
}
