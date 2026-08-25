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
  const regexEscape = (value) => value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
  const readEmbeddedSnapshot = async () => {
    const session = await page.context().newCDPSession(page);
    const response = await session.send('Runtime.evaluate', {
      expression: 'window.__JOB_ANALYTICS__',
      returnByValue: true,
    });
    await session.detach();
    if (response.exceptionDetails) throw new Error('embedded dashboard snapshot unavailable');
    return response.result.value;
  };
  const numberFormat = new Intl.NumberFormat('en-US');
  const percentFormat = new Intl.NumberFormat('en-US', { style: 'percent', maximumFractionDigits: 0 });
  const displayDate = (value) => new Intl.DateTimeFormat('en-US', {
    year: 'numeric', month: 'short', day: 'numeric'
  }).format(new Date(`${value}T00:00:00`));
  const calibrationDisplay = (row) => {
    const responseRate = row.submitted ? row.responded / row.submitted : 0;
    return `${percentFormat.format(responseRate)} response · ${numberFormat.format(row.submitted)} submitted · ${numberFormat.format(row.responded)} responded · ${numberFormat.format(row.interviewed)} interviewed · ${numberFormat.format(row.offered)} offered`;
  };
  const assertCalibrationRow = async (table, scope, row) => {
    const band = row.fit_band ?? row.value;
    const rendered = table.getByRole('row', { name: new RegExp(`^${regexEscape(band)}\\b`) });
    const rowHeader = await rendered.getByRole('rowheader').innerText();
    const value = await rendered.getByRole('cell').innerText();
    check(`${scope} calibration ${band} band identity`, rowHeader === `${band} (n=${numberFormat.format(row.applications)})`, rowHeader);
    check(`${scope} calibration ${band} lifecycle tuple`, value === calibrationDisplay(row), value);
  };
  const assertDataDerivedBaseline = async (scope) => {
    await page.waitForSelector('.pipeline-table tbody tr');
    const data = await readEmbeddedSnapshot();
    const applicationCount = data.meta.record_counts.applications;
    check(`${scope} total application count`, (await page.locator('#pipeline-count').innerText()).startsWith(`${numberFormat.format(applicationCount)} `));
    check(`${scope} five accessible chart families`, await page.locator('svg[role="img"]').count() === 5, String(await page.locator('svg[role="img"]').count()));
    check(`${scope} five chart text equivalents`, await page.locator('.chart-table').count() === 5, String(await page.locator('.chart-table').count()));
    check(`${scope} all chart titles and descriptions`, await page.locator('svg[role="img"] > title').count() === 5 && await page.locator('svg[role="img"] > desc').count() === 5);
    check(`${scope} daily cadence default`, (await page.locator('#time-series-chart svg > title').textContent()).includes('Daily'));
    check(`${scope} explicit axes`, (await page.locator('#time-series-chart').innerText()).includes('Daily Period Start') && (await page.locator('#time-series-chart').innerText()).includes('Submission Count'));
    check(`${scope} sortable table semantics`, await page.locator('th[aria-sort]').count() >= 5);
    for (const chartSpec of [
      { name: /Cumulative Recorded Funnel/, selector: '#funnel-chart', axisTitle: 'Count' },
      { name: /Fit-Band Event Progression/, selector: '#calibration-chart', axisTitle: 'Progression' },
      { name: /Feedback Categories/, selector: '#feedback-category-chart', axisTitle: 'Count' },
      { name: /Pipeline Aging Distribution/, selector: '#aging-chart', axisTitle: 'Count' },
    ]) {
      const chart = page.getByRole('img', { name: chartSpec.name });
      check(`${scope} ${chartSpec.name.source} numeric baseline`, await chart.locator('.axis-line-strong').count() === 1);
      check(`${scope} ${chartSpec.name.source} axis title`, await chart.locator('text.axis').filter({ hasText: new RegExp(`^${regexEscape(chartSpec.axisTitle)}$`) }).isVisible());
      const ticks = chartSpec.axisTitle === 'Progression'
        ? ['0%', '50%', '100%']
        : await page.locator(`${chartSpec.selector} .chart-table tbody td:last-child`).allTextContents().then((values) => {
            const maximum = Math.max(1, ...values.map((value) => Number.parseInt(value.replace(/\D/g, ''), 10)));
            return [...new Set([0, Math.ceil(maximum / 2), maximum])].map(String);
          });
      for (const tick of ticks) {
        const tickLabel = chart.locator('text.axis').filter({ hasText: new RegExp(`^${regexEscape(tick)}$`) });
        check(`${scope} ${chartSpec.name.source} visible tick ${tick}`, await tickLabel.count() === 1 && await tickLabel.isVisible());
      }
    }
    const funnelTable = page.getByRole('table', { name: 'Text Summary: Cumulative Recorded Funnel' });
    const submittedFunnelRow = funnelTable.getByRole('row', { name: /^Submitted\b/ });
    check(`${scope} cumulative submitted count`, await submittedFunnelRow.getByRole('cell').innerText() === numberFormat.format(data.funnel.submitted), await submittedFunnelRow.innerText());
    const globalCalibrationTable = page.getByRole('table', { name: 'Text Summary: Fit-Band Event Progression' });
    const globalBand = data.calibration.fit_band.find((row) => row.applications > 0);
    await assertCalibrationRow(globalCalibrationTable, `${scope} global`, globalBand);
    const dailyRows = page.locator('#time-series-chart .chart-table tbody tr');
    check(`${scope} daily cadence full continuous range`, await dailyRows.count() === data.daily_series.length, String(await dailyRows.count()));
    check(`${scope} daily cadence starts at snapshot start`, (await dailyRows.first().getByRole('rowheader').innerText()) === displayDate(data.daily_series[0].date));
    check(`${scope} daily cadence ends at snapshot date`, (await dailyRows.last().getByRole('rowheader').innerText()) === displayDate(data.daily_series.at(-1).date));
    check(`${scope} daily cadence preserves zero days`, await page.locator('#time-series-chart .chart-table tbody td:last-child').filter({ hasText: /^0$/ }).count() > 0);
    check(`${scope} daily cadence total matches cumulative submitted`, await numericTableTotal('#time-series-chart .chart-table') === data.funnel.submitted);
    await page.selectOption('#cadence-granularity', 'weekly');
    await page.waitForFunction(() => document.querySelector('#time-series-chart svg title')?.textContent.includes('Weekly'));
    check(`${scope} weekly cadence renders`, (await page.locator('#time-series-chart svg > title').textContent()).includes('Weekly'));
    const weekStarts = new Set(data.daily_series.map((row) => {
      const parsed = new Date(`${row.date}T00:00:00Z`);
      parsed.setUTCDate(parsed.getUTCDate() - ((parsed.getUTCDay() + 6) % 7));
      return parsed.toISOString().slice(0, 10);
    }));
    const weeklyRows = page.locator('#time-series-chart .chart-table tbody tr');
    check(`${scope} weekly cadence full continuous range`, await weeklyRows.count() === weekStarts.size, String(await weeklyRows.count()));
    check(`${scope} weekly cadence total matches cumulative submitted`, await numericTableTotal('#time-series-chart .chart-table') === data.funnel.submitted);
    await page.locator('#reset-filters').click();
    return data;
  };

  await page.setViewportSize({ width: 1440, height: 1000 });
  const data = await assertDataDerivedBaseline('real');
  check('desktop filters expanded', await page.locator('#filter-disclosure').getAttribute('open') !== null);

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

  const pairCounts = new Map();
  data.pipeline.forEach((row) => {
    const key = `${row.role_family}\x1f${row.stage}`;
    pairCounts.set(key, (pairCounts.get(key) || 0) + 1);
  });
  const [pairKey, expectedCombinedCount] = [...pairCounts.entries()]
    .find(([, count]) => count > 0 && count < data.pipeline.length);
  const [selectedRoleFamily, selectedStage] = pairKey.split('\x1f');
  await page.selectOption('#role-family', selectedRoleFamily);
  await page.selectOption('#stage', selectedStage);
  await page.waitForFunction((expected) => document.querySelector('#pipeline-count')?.textContent.startsWith(`${expected} `), expectedCombinedCount);
  const combinedCount = Number.parseInt((await page.locator('#pipeline-count').innerText()).replace(/\D/g, ''), 10);
  check('combined filters update all-view count', combinedCount === expectedCombinedCount, String(combinedCount));
  check('combined filters persist in URL', page.url().includes(`role_family=${encodeURIComponent(selectedRoleFamily)}`) && page.url().includes(`stage=${encodeURIComponent(selectedStage)}`), page.url());
  check('screening target remains product constant', data.today.screening_target === 100);
  check('submission capacity remains product constant', data.today.submission_soft_capacity === 20);
  check('today submission semantics remain event-based', (await page.locator('.throughput-band').nth(1).locator('.band-value').innerText()) === `${numberFormat.format(data.today.submitted)} / 20`, await page.locator('.throughput-band').nth(1).locator('.band-value').innerText());
  check('filtered chart families all update', await page.evaluate(() => [...document.querySelectorAll('.chart-shell')].length === 5 && [...document.querySelectorAll('.chart-shell')].every((chart) => chart.childElementCount > 0)));
  const filteredRows = data.pipeline.filter((row) => row.role_family === selectedRoleFamily && row.stage === selectedStage);
  const filteredIds = new Set(filteredRows.map((row) => row.application_id));
  const filteredFunnelTable = page.getByRole('table', { name: 'Text Summary: Cumulative Recorded Funnel' });
  const filteredSubmittedRow = filteredFunnelTable.getByRole('row', { name: /^Submitted\b/ });
  const expectedFilteredSubmitted = data.lifecycle_application_ids.submitted.filter((id) => filteredIds.has(id)).length;
  check('filtered cumulative funnel uses lifecycle IDs', await filteredSubmittedRow.getByRole('cell').innerText() === numberFormat.format(expectedFilteredSubmitted), await filteredSubmittedRow.innerText());
  const filteredCalibrationTable = page.getByRole('table', { name: 'Text Summary: Fit-Band Event Progression' });
  const lifecycleSets = Object.fromEntries(
    ['submitted', 'responded', 'interviewed', 'offered'].map((name) => [name, new Set(data.lifecycle_application_ids[name])])
  );
  const filteredBands = new Map();
  filteredRows.forEach((row) => {
    const ids = filteredBands.get(row.fit_band) || [];
    ids.push(row.application_id);
    filteredBands.set(row.fit_band, ids);
  });
  for (const [fitBand, applicationIds] of filteredBands) {
    await assertCalibrationRow(filteredCalibrationTable, 'filtered', {
      fit_band: fitBand,
      applications: applicationIds.length,
      submitted: applicationIds.filter((id) => lifecycleSets.submitted.has(id)).length,
      responded: applicationIds.filter((id) => lifecycleSets.responded.has(id)).length,
      interviewed: applicationIds.filter((id) => lifecycleSets.interviewed.has(id)).length,
      offered: applicationIds.filter((id) => lifecycleSets.offered.has(id)).length,
    });
  }
  await page.locator('#reset-filters').click();

  const [selectedCategory, categoryApplicationIds] = Object.entries(data.feedback.category_application_ids)
    .find(([category, ids]) => ids.length > 0 && data.feedback.lineage.some((rule) => rule.category === category));
  const categoryIds = new Set(categoryApplicationIds);
  const expectedCategoryCount = data.pipeline.filter((row) => categoryIds.has(row.application_id)).length;
  const categoryRules = data.feedback.lineage.filter((rule) =>
    rule.category === selectedCategory
    && rule.source_feedback.some((source) => categoryIds.has(source.application_id))
  );
  await page.selectOption('#feedback-category', selectedCategory);
  await page.waitForFunction((expected) => document.querySelector('#pipeline-count')?.textContent.startsWith(`${expected} `), expectedCategoryCount);
  check('exact category filtering', (await page.locator('#pipeline-count').innerText()).startsWith(`${expectedCategoryCount} `));
  check('category URL state', page.url().includes(`feedback_category=${encodeURIComponent(selectedCategory)}`), page.url());
  check('category chart isolated', await page.locator('#feedback-category-chart .chart-table tbody tr').count() === 1);
  check('lineage category isolated', await page.locator('details[data-rule-id]').count() === categoryRules.length);
  const lineage = page.locator('details[data-rule-id]').first();
  const firstRule = categoryRules[0];
  const visibleSources = firstRule.source_feedback.filter((source) => categoryIds.has(source.application_id));
  await lineage.locator('summary').click();
  await page.waitForFunction(() => location.search.includes('feedback='));
  const lineageText = await lineage.innerText();
  check('real lineage rule identity', lineageText.includes(firstRule.rule_id), lineageText.slice(0, 300));
  check('real lineage action', lineageText.includes(firstRule.required_action), lineageText.slice(0, 300));
  check('real lineage confidence', lineageText.includes(percentFormat.format(firstRule.confidence)), lineageText.slice(0, 300));
  check('feedback source excerpt visible', lineageText.includes(visibleSources[0].evidence_excerpt), lineageText.slice(-500));
  check('feedback source application ID visible', lineageText.includes(visibleSources[0].application_id), lineageText.slice(-500));
  check('lineage bounded to visible IDs', await lineage.locator('[data-application-id]').count() === visibleSources.length);
  await lineage.locator('[data-application-id]').first().click();
  await page.waitForFunction(() => document.activeElement?.matches('tr[id^="row-"]'));
  check('feedback application action searches pipeline', (await page.locator('#pipeline-search').inputValue()) === visibleSources[0].application_id);
  check('feedback application action focuses row', await page.evaluate(() => document.activeElement?.matches('tr[id^="row-"]')));
  await page.locator('#reset-filters').click();

  const staleQuality = page.locator('#quality-list .quality-item').filter({ hasText: 'Stale Applications' });
  const expectedStaleCount = data.data_quality.stale_rows.length;
  check('all stale targets rendered', await staleQuality.locator('[data-application-id]').count() === expectedStaleCount, String(await staleQuality.locator('[data-application-id]').count()));
  check('all stale targets disclosed by count', (await staleQuality.locator('.quality-targets > summary').innerText()).includes(numberFormat.format(expectedStaleCount)));
  const linkedReviewRows = data.data_quality.review_queue.items.filter((item) => item.candidate_application_ids.length > 0);
  const linkedReviewCandidateCount = linkedReviewRows.reduce((total, item) => total + item.candidate_application_ids.length, 0);
  const linkedReviewApplicationIds = [...new Set(linkedReviewRows.flatMap((item) => item.candidate_application_ids))];
  check('production review rows retain candidate arrays', linkedReviewRows.length > 0 && linkedReviewCandidateCount >= linkedReviewApplicationIds.length, `${linkedReviewRows.length} rows / ${linkedReviewCandidateCount} candidates`);
  const linkedReviewQuality = page.locator('#quality-list .quality-item').filter({ hasText: 'Review Queue, Linked Applications' });
  await linkedReviewQuality.locator('.quality-targets > summary').click();
  check('production review candidates exposed as deduplicated actions', await linkedReviewQuality.locator('[data-application-id]').count() === linkedReviewApplicationIds.length, String(await linkedReviewQuality.locator('[data-application-id]').count()));
  const unmappedReviewQuality = page.locator('#quality-list .quality-item').filter({ hasText: 'Review Queue, Unmapped Items' });
  await unmappedReviewQuality.locator('.quality-targets > summary').click();
  const expectedUnmappedReviews = data.data_quality.review_queue.items.filter((item) => item.candidate_application_ids.length === 0).length;
  check('production unmapped reviews preserve global items', await unmappedReviewQuality.locator('.quality-target').count() === expectedUnmappedReviews, String(await unmappedReviewQuality.locator('.quality-target').count()));
  await page.evaluate(() => document.querySelectorAll('#quality-list .quality-targets').forEach((details) => { details.open = true; }));
  const qualityButtons = page.locator('#quality-list [data-application-id]');
  check('actionable quality applications', await qualityButtons.count() >= expectedStaleCount, String(await qualityButtons.count()));
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
  const searchApplicationId = data.pipeline[0].application_id;
  await page.locator('#pipeline-search').fill(searchApplicationId);
  await page.waitForFunction(() => document.querySelector('#pipeline-count')?.textContent.startsWith('1 '));
  const searchCount = Number.parseInt((await page.locator('#pipeline-count').innerText()).replace(/\D/g, ''), 10);
  check('search narrows results', searchCount === 1, String(searchCount));
  check('search persists in URL', page.url().includes(`search=${encodeURIComponent(searchApplicationId)}`), page.url());
  await page.locator('#pipeline-search').fill('');
  await page.waitForFunction((expected) => document.querySelector('#pipeline-count')?.textContent.startsWith(`${expected} `), data.pipeline.length);
  const pageStatus = await page.locator('#pipeline-pagination').innerText();
  const pageCount = Number.parseInt(pageStatus.match(/Page 1 of ([\d,]+)/)?.[1].replaceAll(',', '') || '1', 10);
  check('pipeline pagination exposes data-derived page count', pageCount >= 2, pageStatus);
  await page.locator('[data-page="next"]').click();
  check('pagination advances', (await page.locator('#pipeline-pagination').innerText()).includes(`Page 2 of ${numberFormat.format(pageCount)}`));
  check('pagination persists in URL', page.url().includes('page=2'), page.url());
  await page.locator('#reset-filters').click();

  await page.locator('#date-start').fill(data.daily_series.at(-1).date);
  await page.locator('#date-start').dispatchEvent('change');
  await page.locator('#date-end').fill(data.daily_series[0].date);
  await page.locator('#date-end').dispatchEvent('change');
  await page.waitForFunction(() => document.querySelector('#pipeline-count')?.textContent.startsWith('0 '));
  check('no-data states appear', await page.locator('.empty-state').count() >= 6, String(await page.locator('.empty-state').count()));
  check('empty states have next action', await page.locator('.empty-state [data-empty-action="reset"]').count() >= 6);
  await page.locator('#reset-filters').click();

  await page.goto(`${dashboard}?role_family=${encodeURIComponent(selectedRoleFamily)}&stage=${encodeURIComponent(selectedStage)}&tab=pipeline&cadence=weekly`);
  await page.waitForSelector('.pipeline-table');
  check('URL restores role filter', await page.locator('#role-family').inputValue() === selectedRoleFamily);
  check('URL restores stage filter', await page.locator('#stage').inputValue() === selectedStage);
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
  const fixtureCandidateIds = ['app-browser-linked', 'app-browser-other'];
  for (const applicationId of fixtureCandidateIds) {
    const currentLinkedReview = page.locator('#quality-list .quality-item').filter({ hasText: 'Review Queue, Linked Applications' });
    await currentLinkedReview.locator('.quality-targets > summary').click();
    check(`linked review application ${applicationId} reachable`, await currentLinkedReview.locator(`[data-application-id="${applicationId}"]`).count() === 1);
    await currentLinkedReview.locator(`[data-application-id="${applicationId}"]`).click();
    await page.waitForFunction((expected) => document.activeElement?.id === `row-${expected}`, applicationId);
    check(`linked review action focuses ${applicationId}`, await page.locator('#pipeline-search').inputValue() === applicationId);
    await page.locator('#reset-filters').click();
  }
  const refreshedUnmappedReview = page.locator('#quality-list .quality-item').filter({ hasText: 'Review Queue, Unmapped Items' });
  await refreshedUnmappedReview.locator('.quality-targets > summary').click();
  check('unmapped review details render', (await refreshedUnmappedReview.innerText()).includes('review-browser-global') && (await refreshedUnmappedReview.innerText()).includes('Resolve this review without an application ID.'));
  const reviewFocusButton = refreshedUnmappedReview.locator('[data-focus-target]').first();
  const reviewTargetId = await reviewFocusButton.getAttribute('data-focus-target');
  await reviewFocusButton.click();
  check('unmapped review action focuses rendered review item', await page.evaluate(() => document.activeElement?.id) === reviewTargetId);

  await page.goto('file:///tmp/task11-dashboard-shifted-fixture.html');
  const shiftedData = await assertDataDerivedBaseline('shifted fixture');
  check('shifted fixture changes application count', shiftedData.meta.record_counts.applications !== data.meta.record_counts.applications);
  check('shifted fixture changes snapshot date', shiftedData.meta.generated_at !== data.meta.generated_at);

  return {
    checks,
    screenshots: ['/tmp/task9-dashboard-desktop.png', '/tmp/task9-dashboard-mobile.png'],
    desktopLayout,
    mobileLayout,
    finalUrl: page.url()
  };
}
