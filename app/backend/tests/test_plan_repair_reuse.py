"""Recompiling an identity correction can reuse the persisted author document."""
from planner_api.plan_ai import FakePlanProvider, SemanticPlanProvider, PlanContext


def test_identity_only_repair_uses_saved_author_but_still_calls_compiler():
    class UnexpectedAuthor:
        def draft_hybrid_document(self, *_args):
            raise AssertionError('An identity-only repair must not regenerate the author')
    class CountingCompiler(FakePlanProvider):
        calls = 0
        def draft_hybrid_details(self, context, *args, **kwargs):
            self.calls += 1
            assert context.source_document == '# 已保存的真实设计稿'
            assert context.planning_context['compiler_identity_correction'] == '只引用原键'
            return super().draft_hybrid_details(context,*args,**kwargs)
    compiler = CountingCompiler()
    context = PlanContext(requirements={'objective':'完成报告'}, route={'stages':[
        {'order':1,'title':'完成报告','purpose':'完成阅读成果','outputs':['报告']}],
        'schedule_type':'OUTCOME'}, source_document='# 已保存的真实设计稿',
        planning_context={'compiler_identity_correction':'只引用原键'})
    outline = FakePlanProvider().draft_hybrid_outline(context).model_dump(mode='json')
    result = SemanticPlanProvider(UnexpectedAuthor(),UnexpectedAuthor(),compiler).draft_hybrid_details(context,outline,1,1)
    assert compiler.calls == 1 and result.days
    assert result.source_document == context.source_document


def test_schema_failure_recompiles_same_author_document():
    from planner_api.ai import ProviderUnavailableError

    class Author:
        calls = 0
        def draft_hybrid_document(self, *_args):
            self.calls += 1
            return '完整设计稿：保留所有任务、阶段容量和前置关系'

    class Compiler(FakePlanProvider):
        calls = 0
        def draft_hybrid_details(self, context, *args, **kwargs):
            self.calls += 1
            assert context.source_document == '完整设计稿：保留所有任务、阶段容量和前置关系'
            if self.calls == 1:
                raise ProviderUnavailableError('字段非法', code='INVALID_MODEL_RESPONSE', diagnostic='缺少字段')
            assert context.planning_context['generation_validation_feedback']['diagnostic'] == '缺少字段'
            return super().draft_hybrid_details(context, *args, **kwargs)

    context = PlanContext(requirements={'objective': '报告'}, route={'stages': [
        {'order': 1, 'title': '报告', 'purpose': '完成阅读报告', 'outputs': ['报告']}],
        'schedule_type': 'OUTCOME'})
    outline = FakePlanProvider().draft_hybrid_outline(context).model_dump(mode='json')
    author, compiler = Author(), Compiler()
    result = SemanticPlanProvider(author, author, compiler).draft_hybrid_details(context, outline, 1, 1)
    assert author.calls == 1 and compiler.calls == 2 and result.days


def test_reference_repair_keeps_complete_error_then_escalates_on_repeat():
    from fastapi import HTTPException
    from planner_api.plan_service import PlanService

    context = PlanContext(requirements={'capacity': 85}, route={'stages': []})
    detail = {'code': 'PLAN_WEEK_DEPENDENCY_INVALID', 'conflicts': [
        {'task': '报告', 'dependency': 'M1-W1-D1-T9'}],
        'valid_task_references': ['M1-W1-D1-T1']}
    first = PlanService._business_retry_context(context, 'DETAILS', 1, HTTPException(503, detail), '设计稿')
    assert first.source_document == '设计稿'
    assert first.planning_context['generation_validation_feedback']['validation_detail'] == detail
    second = PlanService._business_retry_context(first, 'DETAILS', 2, HTTPException(503, detail), '设计稿')
    assert second.source_document is None
    assert second.requirements == context.requirements


def test_content_and_capacity_failures_do_not_reuse_design():
    from fastapi import HTTPException
    from planner_api.plan_service import PlanService
    from planner_api.work_item_validation import WorkItemValidationError

    context = PlanContext(requirements={'capacity': 85}, route={})
    for error in [HTTPException(503, {'code': 'PLAN_COMMITMENT_MINUTES_INVALID'}),
                  HTTPException(503, {'code': 'PLAN_STAGE_CONTRIBUTION_INVALID'}),
                  WorkItemValidationError('HYBRID_WORK_ITEM_BATCH_INVALID', ('依赖边重复: A -> B', '缺少工作项'))]:
        retry = PlanService._business_retry_context(context, 'DETAILS', 1, error, '未通过业务约束的设计稿')
        assert retry.source_document is None and retry.requirements == context.requirements


def test_registered_dependency_reuse_preserves_semantic_changes_and_new_edges():
    from planner_api.plan_service import PlanService
    from planner_api.work_item_domain import WorkItemDependencyDraft
    edge = WorkItemDependencyDraft(predecessor_key='REPORT1', successor_key='ACCEPTANCE',
        minimum_lag_days=1, maximum_lag_days=3, condition={'approved': True}, source_refs=['用户确认'])
    registry = [edge.model_dump(mode='json', exclude={'source_refs'})]
    for field, value in [('minimum_lag_days', 2), ('maximum_lag_days', 4),
                         ('condition', {'approved': False}), ('successor_key', 'REPORT2')]:
        changed = edge.model_copy(update={field: value})
        dependencies = [edge, changed]
        assert PlanService._reuse_registered_dependencies(dependencies, registry) == 1
        assert dependencies == [changed]
        assert registry == [edge.model_dump(mode='json', exclude={'source_refs'})]


def test_repeated_schema_failure_escalates_to_author():
    from planner_api.ai import ProviderUnavailableError
    class Author:
        calls = 0
        def draft_hybrid_document(self, context, *_args):
            self.calls += 1
            if self.calls == 2:
                assert context.planning_context['generation_validation_feedback']['attempt'] == 2
            return f'完整设计稿版本{self.calls}'
    class Compiler(FakePlanProvider):
        calls = 0
        def draft_hybrid_outline(self, context, *args, **kwargs):
            self.calls += 1
            if self.calls < 3:
                assert context.source_document == '完整设计稿版本1'
                raise ProviderUnavailableError('字段非法', code='INVALID_MODEL_RESPONSE', diagnostic='语义无法编译')
            assert context.source_document == '完整设计稿版本2'
            return super().draft_hybrid_outline(context, *args, **kwargs)
    context = PlanContext(requirements={'objective': '报告'}, route={'stages': [
        {'order': 1, 'title': '报告', 'purpose': '完成阅读报告', 'outputs': ['报告']}],
        'schedule_type': 'OUTCOME'})
    author, compiler = Author(), Compiler()
    result = SemanticPlanProvider(author, author, compiler).draft_hybrid_outline(context)
    assert author.calls == 2 and compiler.calls == 3 and result.source_document == '完整设计稿版本2'
