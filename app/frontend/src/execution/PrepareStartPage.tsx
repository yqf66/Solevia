import { useEffect, useState } from 'react'
import { CalendarClock, ChevronLeft, ChevronRight, ListChecks } from 'lucide-react'
import { SmartCalendarControl } from '../ExecutionDateControls'
import { ScheduleOptions } from '../ScheduleOptions'
import { useNavigate, useParams } from 'react-router-dom'
import { allocateEntirePlan, getCandidatePools, getExecutionSetup, getWorkspaceScheduleProfile, updateWorkspaceScheduleProfile, getWorkspaceHolidays, getHolidayRegionSuggestion, previewExecution, startExecution, type CandidatePool, type ExecutionPreview, type ExecutionScheduleInput, type ExecutionSetup, type SmartCalendar, type WorkspaceScheduleProfile, type HolidayCalendarDay, type HolidayRegionSuggestion } from '../api'
import { dateInputValue, dateInputInTimezone, detectedHolidayRegion, scheduleTypeLabels, holidayCountryNames, holidayCountryName, weekdayLabels, friendlyDate } from '../ui/plannerPresentation'
import { OperationProgress } from '../ui/ProcessDetails'
import { StageTransition, PlannerStepper } from '../ui/PlannerStage'
import { groupCalendarPreviewWeeks } from './calendarPreview'

export function PrepareStartPage() {
  const { goalId } = useParams()
  const navigate = useNavigate()
  const [setup, setSetup] = useState<ExecutionSetup | null>(null)
  const [startDate, setStartDate] = useState(dateInputValue())
  const [weekdays, setWeekdays] = useState<number[]>([])
  const [excludedDates, setExcludedDates] = useState<string[]>([])
  const [excludedDraft, setExcludedDraft] = useState('')
  const [preview, setPreview] = useState<ExecutionPreview | null>(null)
  const [error, setError] = useState('')
  const [isStarting, setIsStarting] = useState(false)
  const [calendarStarted, setCalendarStarted] = useState(false)
  const [smartCalendar, setSmartCalendar] = useState<SmartCalendar | null>(null)
  const [smartCalendarBusy, setSmartCalendarBusy] = useState(false)
  const [previewWeekIndex, setPreviewWeekIndex] = useState(0)
  const [dateMode, setDateMode] = useState<'SMART' | 'MANUAL'>('SMART')
  const [scheduleBusy, setScheduleBusy] = useState(false)
  const [scheduleResult, setScheduleResult] = useState<Awaited<ReturnType<typeof allocateEntirePlan>> | null>(null)
  const [holidayProfile, setHolidayProfile] = useState<WorkspaceScheduleProfile | null>(null)
  const [holidaySuggestion, setHolidaySuggestion] = useState<HolidayRegionSuggestion | null>(null)
  const [mappedHolidays, setMappedHolidays] = useState<HolidayCalendarDay[]>([])
  const [holidayCoverage, setHolidayCoverage] = useState<{ status: 'COMPLETE' | 'PARTIAL' | 'UNAVAILABLE'; missingYears: number[] } | null>(null)
  const [candidatePools, setCandidatePools] = useState<CandidatePool[]>([])
  const [selectedCandidateIds, setSelectedCandidateIds] = useState<string[]>([])
  const requiredExecutionDays = setup?.minimum_execution_days ?? setup?.weekly_execution_days ?? 1
  const previewDays = smartCalendar?.days.length ? smartCalendar.days : preview?.upcoming_days ?? []
  const previewWeeks = groupCalendarPreviewWeeks(previewDays)
  const activePreviewWeekIndex = Math.min(previewWeekIndex, Math.max(previewWeeks.length - 1, 0))
  const activePreviewWeek = previewWeeks[activePreviewWeekIndex]

  useEffect(() => {
    if (!goalId) return
    let active = true
    getExecutionSetup(goalId).then((result) => {
      if (!active) return
      if (result.status === 'ACTIVE') {
        navigate(`/goals/${goalId}/today`, { replace: true })
        return
      }
      setSetup(result)
      setStartDate(result.suggested_start_date ?? result.current_date ?? dateInputInTimezone(result.timezone))
      setWeekdays(result.suggested_weekdays)
      if (result.has_daily_plan === false) setDateMode('MANUAL')
      getCandidatePools(goalId).then((pools) => {
        if (!active) return
        setCandidatePools(pools)
        setSelectedCandidateIds(pools.flatMap((pool) => pool.selected_item_ids))
      }).catch(() => undefined)
    }).catch((caught) => {
      if (active) setError(caught instanceof Error ? caught.message : '暂时无法读取开始设置。')
    })
    return () => { active = false }
  }, [goalId, navigate])

  useEffect(() => {
    let active = true
    getWorkspaceScheduleProfile().then(async (profile) => {
      const detected = detectedHolidayRegion()
      const suggestion = await getHolidayRegionSuggestion(detected.timezone, detected.locale)
      if (active) setHolidaySuggestion(suggestion)
      const resolved = profile.holiday_country_code || !suggestion.country_code || suggestion.requires_confirmation
        ? profile : await updateWorkspaceScheduleProfile({
          timezone: detected.timezone,
          holiday_country_code: suggestion.country_code,
          holiday_locale: detected.locale,
          holiday_source: 'AUTO',
          holiday_confidence: suggestion.confidence,
          holiday_confirmed: false,
        })
      if (active) setHolidayProfile(resolved)
    }).catch(() => undefined)
    return () => { active = false }
  }, [])

  useEffect(() => {
    if (!goalId || !setup || dateMode !== 'MANUAL' || smartCalendarBusy || smartCalendar || weekdays.length !== requiredExecutionDays) {
      return
    }
    let active = true
    const timer = window.setTimeout(() => {
      previewExecution(goalId, {
        requested_start_date: startDate,
        execution_weekdays: weekdays,
        excluded_dates: excludedDates,
        selected_work_item_ids: selectedCandidateIds,
      }).then((result) => {
        if (active) { setPreview(result); setPreviewWeekIndex(0); setError('') }
      }).catch((caught) => {
        if (active) { setPreview(null); setError(caught instanceof Error ? caught.message : '暂时无法预览日期。') }
      })
    }, 180)
    return () => { active = false; window.clearTimeout(timer) }
  }, [excludedDates, goalId, holidayProfile?.holiday_country_code, setup, startDate, weekdays, smartCalendar, smartCalendarBusy, dateMode, requiredExecutionDays, selectedCandidateIds])

  useEffect(() => {
    if (!preview || !holidayProfile?.holiday_country_code) {
      setMappedHolidays([])
      setHolidayCoverage(null)
      return
    }
    let active = true
    getWorkspaceHolidays(startDate, preview.projected_end_date).then((result) => {
      if (!active) return
      setMappedHolidays(Array.isArray(result?.days) ? result.days : [])
      setHolidayCoverage({
        status: result?.coverage ?? 'UNAVAILABLE',
        missingYears: Array.isArray(result?.missing_years) ? result.missing_years : [],
      })
    }).catch(() => {
      if (active) {
        setMappedHolidays([])
        setHolidayCoverage({ status: 'UNAVAILABLE', missingYears: [] })
      }
    })
    return () => { active = false }
  }, [holidayProfile?.holiday_country_code, preview, startDate])

  async function changeHolidayCountry(countryCode: string) {
    setSmartCalendar(null)
    const detected = detectedHolidayRegion()
    const result = await updateWorkspaceScheduleProfile({
      holiday_country_code: countryCode,
      holiday_locale: detected.locale,
      holiday_source: 'USER',
      holiday_confidence: 1,
      holiday_confirmed: true,
    })
    setHolidayProfile(result)
  }

  function toggleWeekday(day: number) {
    if (!setup) return
    setSmartCalendar(null)
    setWeekdays((current) => current.includes(day)
      ? current.filter((item) => item !== day)
      : current.length < requiredExecutionDays
        ? [...current, day].sort((a, b) => a - b)
        : current)
  }

  function addExcludedDate() {
    if (!excludedDraft || excludedDates.includes(excludedDraft)) return
    setSmartCalendar(null)
    setExcludedDates((current) => [...current, excludedDraft].sort())
    setExcludedDraft('')
  }

  async function handleStart() {
    if (!goalId || !setup || !preview || isStarting || smartCalendarBusy || (smartCalendar ? !smartCalendar.can_apply : weekdays.length !== requiredExecutionDays)) return
    const input: ExecutionScheduleInput = smartCalendar?.input ?? {
      requested_start_date: startDate,
      execution_weekdays: weekdays,
      excluded_dates: excludedDates,
      materialization_preview_id: preview.materialization_preview_id,
      selected_work_item_ids: selectedCandidateIds,
    }
    setIsStarting(true)
    setError('')
    try {
      await startExecution(goalId, input)
      setCalendarStarted(true)
      setIsStarting(false)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '暂时无法开始计划。')
      setIsStarting(false)
    }
  }

  async function generateDailyTimes() {
    if (!goalId || scheduleBusy) return
    setScheduleBusy(true); setError('')
    try { setScheduleResult(await allocateEntirePlan(goalId)) }
    catch (caught) { setError(caught instanceof Error ? caught.message : '每日具体时间生成失败，你可以稍后在执行页重试。') }
    finally { setScheduleBusy(false) }
  }

  if (!setup && !error) return <StageTransition current={3} title="正在准备开始" detail="Solevia 正在把已经确认的安排放进日历，不会改变刚才确认的内容。" checkpoints={['回顾已经确认的安排', '准备推荐执行日', '预览接下来的日期']} />
  if (!setup) return <section className="page prepare-unavailable"><div className="setup-section" role="alert"><h1>暂时无法准备开始</h1><p>{error}</p><button type="button" className="button" onClick={() => navigate(`/goals/${goalId}/plan`)}>返回时间计划</button></div></section>

  return (
    <section className="page planner-stage-page prepare-page">
      <div className="planner-primary-column">
        <header className="planner-stage-header">
          <div className="stage-heading"><p className="eyebrow">第 4 步 · 准备开始</p><h1>{setup?.schedule_type === 'OUTCOME' ? '设定成果窗口的检查节奏' : '把相对计划放进日历'}</h1></div>
          <PlannerStepper current={3} viewing={3} onStepSelect={(index) => {
            if (!goalId) return
            if (index === 0) navigate(`/goals/${goalId}/clarify`)
            if (index === 1) navigate(`/goals/${goalId}/route`)
            if (index === 2) navigate(`/goals/${goalId}/plan`)
          }} />
        </header>
        <div className="stage-main-scroll">
          <div className="prepare-main">
            {goalId && setup?.schedule_type !== 'OUTCOME' && <ScheduleOptions goalId={goalId} />}
            <section className="prepare-intro"><p className="eyebrow">计划已经确认 · {setup ? scheduleTypeLabels[setup.schedule_type] : '准备中'}</p><h2>什么时候开始，由你决定</h2><p>{setup?.schedule_type === 'OUTCOME' ? '开始日期用于打开第一个成果窗口；检查日只是回顾目标的时间，不代表必须等到那天才行动。' : '这里只把计划放进日历，不会把建议时间变成硬性闹钟。以后临时有事，计划仍然可以弹性调整。'}</p></section>
            {setup && <>
              {setup.schedule_type !== 'OUTCOME' && <section className="setup-section holiday-setup-section">
                <header><span>✓</span><div><h3>法定节假日日历</h3><p>节假日默认跳过；如果讨论中明确“假期只休部分日期”，会按已确认规则保留其余执行日。中国大陆调休上班日可正常执行。</p></div></header>
                <div className="holiday-profile-control">
                  <label><span>当前地区</span><select aria-label="法定节假日地区" value={holidayProfile?.holiday_country_code ?? ''} onChange={(event) => void changeHolidayCountry(event.target.value)}>
                    <option value="" disabled>请选择地区</option>
                    {Array.from(new Set([
                      ...Object.keys(holidayCountryNames),
                      holidaySuggestion?.timezone_country_code,
                      holidaySuggestion?.locale_country_code,
                    ].filter((code): code is string => Boolean(code)))).map((code) => <option value={code} key={code}>{holidayCountryName(code)}</option>)}
                  </select></label>
                  <div><strong>{holidayProfile?.holiday_country_code ? `${holidayCountryName(holidayProfile.holiday_country_code)}日历已启用` : holidaySuggestion?.reason === 'TIMEZONE_LOCALE_CONFLICT' ? '本机时区与系统地区不一致，请选择' : '尚未启用节假日日历'}</strong><small>{holidayProfile?.holiday_source === 'AUTO' ? `根据本机时区和地区自动推荐${holidayProfile.holiday_confirmed ? '，已确认' : '，可在左侧修改'}` : holidayProfile?.holiday_source === 'USER' ? '你已手动选择，后续规划会继续使用' : holidaySuggestion?.country_code ? `建议采用${holidayCountryName(holidaySuggestion.country_code)}，确认后会保存` : '选择后会保存为工作区偏好'}</small></div>
                </div>
                {mappedHolidays.length > 0 && <p className="holiday-range-summary">当前计划范围内识别到 {mappedHolidays.filter((item) => item.kind === 'HOLIDAY').length} 个休息日、{mappedHolidays.filter((item) => item.kind === 'WORKDAY').length} 个调休工作日。</p>}
                {holidayCoverage && holidayCoverage.status !== 'COMPLETE' && <p className="holiday-coverage-warning">{holidayCoverage.missingYears.length ? `${holidayCoverage.missingYears.join('、')} 年的完整法定安排尚未取得，这些年份暂时只按每周执行日计算。` : '暂时无法取得该地区的节假日数据，这次预览只按每周执行日计算。'}</p>}
              </section>}
              <OperationProgress active={isStarting} scope={goalId} label="正在建立执行日历并保存安排" error={error} />
              {!calendarStarted && setup.has_daily_plan !== false && <div className="date-mode-control" role="group" aria-label="日期安排方式"><button type="button" className={dateMode === 'SMART' ? 'selected' : ''} disabled={smartCalendarBusy} onClick={() => { setDateMode('SMART'); setPreview(null); setSmartCalendar(null); setPreviewWeekIndex(0) }}><CalendarClock size={18} />智能排布</button><button type="button" className={dateMode === 'MANUAL' ? 'selected' : ''} disabled={smartCalendarBusy} onClick={() => { setDateMode('MANUAL'); setSmartCalendar(null); setWeekdays(setup.suggested_weekdays); setPreview(null); setPreviewWeekIndex(0) }}><ListChecks size={18} />手动日期</button></div>}
              {candidatePools.length > 0 && !calendarStarted && <section className="setup-section candidate-pool-setup" aria-label="候选任务池"><header><span>候</span><div><h3>选择候选任务</h3><p>每组只需完成规定数量；未选中的候选不会被记为失败，也不会生成欠账。</p></div></header>{candidatePools.map((pool) => { const selectedCount = pool.candidates.filter((item) => selectedCandidateIds.includes(item.id)).length; return <fieldset key={pool.id}><legend>{pool.title} · 选择 {pool.minimum_selected}{pool.maximum_selected ? `–${pool.maximum_selected}` : '+'} 项（当前 {selectedCount}）</legend><div className="candidate-pool-options">{pool.candidates.map((item) => { const checked = selectedCandidateIds.includes(item.id); const disableMore = !checked && pool.maximum_selected !== null && selectedCount >= pool.maximum_selected; return <label key={item.id} className={checked ? 'selected' : ''}><input type="checkbox" checked={checked} disabled={disableMore || smartCalendarBusy} onChange={() => setSelectedCandidateIds((current) => checked ? current.filter((id) => id !== item.id) : [...current, item.id])} /><span><strong>{item.title}</strong><small>{item.estimate_minutes} 分钟 · {item.description}</small></span></label> })}</div>{selectedCount < pool.minimum_selected && <small className="form-error">还需要选择 {pool.minimum_selected - selectedCount} 项。</small>}</fieldset> })}</section>}
              {!calendarStarted && setup.has_daily_plan !== false && dateMode === 'SMART' && goalId && <SmartCalendarControl goalId={goalId} startDate={startDate} result={smartCalendar} onStartDate={setStartDate} onManual={() => { setDateMode('MANUAL'); setSmartCalendar(null); setWeekdays(setup.suggested_weekdays); setPreview(null); setPreviewWeekIndex(0) }} onBusy={setSmartCalendarBusy} onResult={(result) => { setSmartCalendar(result); setPreviewWeekIndex(0); setError(''); if (result) { setStartDate(result.input.requested_start_date); setWeekdays(result.input.execution_weekdays); setExcludedDates(result.input.excluded_dates); setPreview(result.preview) } else { setWeekdays(setup.suggested_weekdays); setPreview(null) } }} />}
              {dateMode === 'MANUAL' && <section className="setup-section">
                <header><span>1</span><div><h3>选择开始日期</h3><p>{setup.schedule_type === 'OUTCOME' ? '从这一天开始，你随时可以推进第一个成果目标。' : '如果这一天不是执行日，首个任务会落在之后最近的执行日。'}</p></div></header>
                <div className="date-shortcuts"><button type="button" disabled={smartCalendarBusy} onClick={() => { setSmartCalendar(null); setStartDate(dateInputInTimezone(setup.timezone)) }}>今天</button><button type="button" disabled={smartCalendarBusy} onClick={() => { setSmartCalendar(null); setStartDate(dateInputInTimezone(setup.timezone, 1)) }}>明天</button><input type="date" aria-label="实际开始日期" value={startDate} disabled={smartCalendarBusy} onChange={(event) => { setSmartCalendar(null); setStartDate(event.target.value) }} /></div>
              </section>}
              {dateMode === 'MANUAL' && <section className="setup-section">
                <header><span>2</span><div><h3>{setup.has_phased_capacity ? '选择各阶段可用的星期' : setup.schedule_type === 'OUTCOME' ? '选择每周检查日' : '选择每周执行日'}</h3><p>{setup.has_phased_capacity ? `预选 ${requiredExecutionDays} 天用于覆盖后期容量；各周仍按自己的阶段天数和分钟上限安排。成果检查点不代表只有一天可用。` : setup.schedule_type === 'OUTCOME' ? '这一天用于回看窗口成果；目标在整个窗口内都可以完成。' : requiredExecutionDays > setup.weekly_execution_days ? `计划摘要原为每周 ${setup.weekly_execution_days} 天；结合任务频率与间隔后，至少需要 ${requiredExecutionDays} 个执行日，已预选可行组合。` : `这份计划每周需要 ${requiredExecutionDays} 个执行日，已按任务间隔预选。`}</p></div></header>
                <div className="weekday-picker">{weekdayLabels.map((label, day) => <button className={weekdays.includes(day) ? 'selected' : ''} type="button" disabled={smartCalendarBusy || Boolean(smartCalendar)} onClick={() => toggleWeekday(day)} key={label}>{label}</button>)}</div>
                <small>{setup.schedule_type === 'OUTCOME' && !setup.has_phased_capacity ? '选择 1 个检查日，不会锁定具体行动日' : `已选择 ${weekdays.length} / ${requiredExecutionDays} 天`}</small>
              </section>}
              {dateMode === 'MANUAL' && setup.schedule_type !== 'OUTCOME' && <section className="setup-section optional">
                <header><span>3</span><div><h3>已知无法执行的日期（可选）</h3><p>旅行、考试或其他确定冲突可以先跳过，不会形成历史欠账。</p></div></header>
                <div className="excluded-date-input"><input type="date" aria-label="无法执行日期" value={excludedDraft} onChange={(event) => setExcludedDraft(event.target.value)} /><button type="button" onClick={addExcludedDate}>添加</button></div>
                {excludedDates.length > 0 && <div className="excluded-date-list">{excludedDates.map((item) => <button type="button" onClick={() => setExcludedDates((current) => current.filter((value) => value !== item))} key={item}>{friendlyDate(item)} ×</button>)}</div>}
              </section>}
              {(smartCalendar || weekdays.length === requiredExecutionDays) && preview && <section className="calendar-preview">
                <header><div><p className="eyebrow">{setup.schedule_type === 'OUTCOME' ? '成果窗口预览' : '日期预览'}</p><h3>{friendlyDate(preview.first_execution_date)}开始，预计 {friendlyDate(preview.projected_end_date)}完成</h3></div><span>{preview.total_execution_days} 个{setup.schedule_type === 'OUTCOME' ? '检查点' : '执行日'}</span></header>
                {activePreviewWeek && <>
                  <nav className="calendar-preview-pagination" aria-label="按周查看日期安排">
                    <button type="button" aria-label="查看上一周" title="上一周" disabled={activePreviewWeekIndex === 0} onClick={() => setPreviewWeekIndex(activePreviewWeekIndex - 1)}><ChevronLeft size={18} /></button>
                    <label><span>周次</span><select aria-label="选择预览周次" value={activePreviewWeekIndex} onChange={(event) => setPreviewWeekIndex(Number(event.target.value))}>{previewWeeks.map((week, index) => <option value={index} key={week.key}>第 {index + 1} 周 · {friendlyDate(week.startDate)} - {friendlyDate(week.endDate)}</option>)}</select></label>
                    <span>{activePreviewWeek.days.length} 个{setup.schedule_type === 'OUTCOME' ? '检查点' : '执行日'}</span>
                    <button type="button" aria-label="查看下一周" title="下一周" disabled={activePreviewWeekIndex === previewWeeks.length - 1} onClick={() => setPreviewWeekIndex(activePreviewWeekIndex + 1)}><ChevronRight size={18} /></button>
                  </nav>
                  <div className="calendar-preview-days">{activePreviewWeek.days.map((item) => <article key={item.plan_day_id}><time>{friendlyDate(item.scheduled_date)}</time><strong>{item.code}</strong><p>{item.primary_goal}</p></article>)}</div>
                </>}
              </section>}
              {calendarStarted && (setup.schedule_type === 'OUTCOME' ? <section className="prepare-allocation-choice"><p className="eyebrow">成果窗口已开启</p><h3>窗口内自由行动</h3><p>当前目标会持续展示到你完成验收，不会因为今天没有拍摄就判定失败。</p><div className="prepare-actions"><button className="button primary" type="button" onClick={() => navigate(`/goals/${goalId}/today`)}>查看当前成果目标 →</button></div></section> : <section className="prepare-allocation-choice" aria-label="每日具体时间安排"><p className="eyebrow">日期规划已保存</p><h3>要现在为每天分配具体时间吗？</h3><p>Solevia 会逐日读取当天任务、固定安排、可用时段与个人偏好，分别生成时间安排。各天独立处理，不会让一次模型调用同时决定整份计划。</p>{scheduleResult && <div className="prepare-allocation-result"><strong>已处理 {scheduleResult.scheduled_days} 个执行日</strong><span>{scheduleResult.assigned_tasks} 项已安排 · {scheduleResult.unallocated_tasks} 项保持自由</span>{scheduleResult.unallocated_tasks > 0 && <small>无法找到完整空档的任务不会被硬塞进日程，进入执行页后仍可手动安排。</small>}</div>}<div className="prepare-actions"><button className="button" type="button" disabled={scheduleBusy} onClick={() => navigate(`/goals/${goalId}/today`)}>暂时保持自由安排</button>{!scheduleResult && <button className="button primary" type="button" disabled={scheduleBusy} onClick={() => void generateDailyTimes()}>{scheduleBusy ? '正在逐日安排…' : '为每日任务生成具体时间'}</button>}{scheduleResult && <button className="button primary" type="button" onClick={() => navigate(`/goals/${goalId}/today`)}>进入每日执行 →</button>}</div></section>)}
              {error && <div role="alert"><p className="form-error">{error}</p><button type="button" className="button ghost" onClick={() => navigate(`/goals/${goalId}/plan`)}>返回讨论，调整日期或可用时间</button></div>}
              {!calendarStarted && <div className="prepare-actions"><button className="button" type="button" onClick={() => navigate(`/goals/${goalId}/plan`)}>回看时间计划</button><button className="button primary" type="button" disabled={!preview || isStarting || smartCalendarBusy || (smartCalendar ? !smartCalendar.can_apply : weekdays.length !== requiredExecutionDays)} onClick={() => void handleStart()}>{isStarting ? '正在创建…' : setup.schedule_type === 'OUTCOME' ? '开启成果窗口 →' : '确认日期安排 →'}</button></div>}
            </>}
          </div>
        </div>
      </div>
      <aside className="stage-context-column prepare-context"><div className="panel"><h2>这一步会确定</h2><div className="facts-preview"><div><span>开始日期</span><p>{startDate ? friendlyDate(startDate) : '等你选择'}</p></div><div><span>{setup?.schedule_type === 'OUTCOME' ? '成果窗口' : '计划周期'}</span><p>{setup?.total_weeks ?? '—'} {setup?.schedule_type === 'OUTCOME' ? '个' : '周'}</p></div><div><span>{setup?.schedule_type === 'OUTCOME' ? '检查方式' : '每周节奏'}</span><p>{setup?.schedule_type === 'OUTCOME' ? '每窗口一次回顾' : `${requiredExecutionDays} 个执行日`}</p></div></div></div><div className="panel"><h2>仍然保持弹性</h2><p className="muted">以后临时有事、任务偏难或长期节奏不合适，都可以反馈并调整。</p></div></aside>
    </section>
  )
}
