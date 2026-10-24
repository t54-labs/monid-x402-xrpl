"""Regression checks for additive managed-policy access and fail-closed verification.

The small evaluator below only supports the unconditional identity statements
used here. Use check_parameter_access.py for authoritative AWS role simulation.
"""

import fnmatch
import json
import re
import sys
from pathlib import Path

import pytest

from scripts import check_parameter_access
from scripts.check_parameter_access import access_cases, verify_results

ROOT = Path(__file__).parents[1]
ACCOUNT = "111111111111"
REGION = "us-east-1"
PREFIX = "/example/runtime"
ROLE = f"arn:aws:iam::{ACCOUNT}:role/example"
BASE = f"arn:aws:ssm:{REGION}:{ACCOUNT}:parameter"


def resolve(value):
    substitutions = {"AWS::Partition": "aws", "AWS::Region": REGION,
                     "AWS::AccountId": ACCOUNT, "ParameterPrefix": PREFIX}
    if isinstance(value, dict):
        if set(value) == {"Fn::Sub"}:
            return re.sub(r"\$\{([^}]+)\}", lambda match: substitutions[match[1]], value["Fn::Sub"])
        return {key: resolve(item) for key, item in value.items()}
    if isinstance(value, list):
        return [resolve(item) for item in value]
    return value


def statements():
    service = json.loads((ROOT / "deploy/aws/service.json").read_text())
    automation = json.loads((ROOT / "deploy/aws/automation.json").read_text())
    policies = service["Resources"]["InstanceRole"]["Properties"]["Policies"]
    restriction = next(p["PolicyDocument"] for p in policies
                       if p["PolicyName"] == "RestrictRuntimeParameters")
    runtime = automation["Resources"]["InstanceRuntimePolicy"]["Properties"]["PolicyDocument"]
    parameter_allows = [s for s in runtime["Statement"] if "ssm:" in str(s["Action"])]
    # Exact conflicting statement subset from AmazonSSMManagedInstanceCore v2:
    # https://docs.aws.amazon.com/aws-managed-policy/latest/reference/AmazonSSMManagedInstanceCore.html
    managed = {"Effect": "Allow", "Action": ["ssm:GetParameter", "ssm:GetParameters",
                                              "ssm:UpdateInstanceInformation", "ssm:GetDocument",
                                              "ssm:ListInstanceAssociations"], "Resource": "*"}
    return [managed, *resolve(parameter_allows), *resolve(restriction["Statement"])]


def matches(value, patterns):
    return any(fnmatch.fnmatchcase(value, pattern) for pattern in
               (patterns if isinstance(patterns, list) else [patterns]))


def decision(policies, action, resource):
    effects = []
    for statement in policies:
        assert set(statement) <= {"Sid", "Effect", "Action", "Resource", "NotResource"}
        if not matches(action, statement["Action"]):
            continue
        selected = (matches(resource, statement["Resource"]) if "Resource" in statement
                    else not matches(resource, statement["NotResource"]))
        if selected:
            effects.append(statement["Effect"])
    return "explicitDeny" if "Deny" in effects else "allowed" if "Allow" in effects else "implicitDeny"


@pytest.mark.parametrize("action,resource,expected", [case for case in access_cases(ROLE, REGION, PREFIX)
                                                    if case[0].startswith("ssm:")])
def test_combined_managed_and_inline_permissions(action, resource, expected):
    assert decision(statements(), action, resource) == expected


def test_scoped_allow_alone_does_not_restrict_managed_policy():
    allows = [statement for statement in statements() if statement["Effect"] == "Allow"]
    for action in ("ssm:GetParameter", "ssm:GetParameters"):
        assert decision(allows, action, BASE + "/another-service/SECRET") == "allowed"
        assert decision(statements(), action, BASE + "/another-service/SECRET") == "explicitDeny"


def test_deny_overrides_future_wildcard_grants_including_recursive_ancestors():
    policies = [*statements(), {"Effect": "Allow", "Action": "ssm:Get*", "Resource": "*"}]
    for action, resource, expected in access_cases(ROLE, REGION, PREFIX):
        if expected == "explicitDeny":
            assert decision(policies, action, resource) == expected
    # Also check the root ARN that the AWS simulator cannot model. Deployment
    # verification must exercise this path using the actual instance role.
    assert decision(policies, "ssm:GetParametersByPath", BASE + "/") == "explicitDeny"


@pytest.mark.parametrize("filename", ["service.json", "automation.json"])
def test_prefix_cannot_broaden_to_root_or_wildcards(filename):
    parameter = json.loads((ROOT / "deploy/aws" / filename).read_text())["Parameters"]["ParameterPrefix"]
    assert parameter["Default"] == "/monid-x402-xrpl/runtime"
    assert re.fullmatch(parameter["AllowedPattern"], PREFIX)
    for unsafe in ("/", "//", "/example/", "/example/*", "/example//runtime", "/example/?"):
        assert not re.fullmatch(parameter["AllowedPattern"], unsafe)


def test_verifier_rejects_missing_ambiguous_or_wrong_aws_results():
    cases = [("ssm:GetParameter", BASE + "/outside/KEY", "explicitDeny")]
    result = {"EvalActionName": cases[0][0], "EvalResourceName": cases[0][1], "EvalDecision": "explicitDeny"}
    verify_results(cases, [result])
    for results in ([], [result, result], [{**result, "EvalDecision": "allowed"}],
                    [{**result, "EvalDecision": "implicitDeny"}],
                    [{**result, "MissingContextValues": ["aws:PrincipalTag/Team"]}]):
        with pytest.raises(ValueError):
            verify_results(cases, results)


def test_simulator_forwards_explicit_context_for_conditional_role_policies(monkeypatch, capsys):
    context = [{"ContextKeyName": "cloudwatch:namespace", "ContextKeyValues": ["Monid/Gateway"],
                "ContextKeyType": "string"}]
    monkeypatch.setattr(sys, "argv", ["check_parameter_access.py", "--role-arn", ROLE,
                                     "--region", REGION, "--prefix", PREFIX,
                                     "--context-entries", json.dumps(context)])
    expected = {(action, resource): decision
                for action, resource, decision in access_cases(ROLE, REGION, PREFIX)}
    simulated = []

    def aws_json(arguments):
        if arguments[0] == "sts":
            return {"Account": ACCOUNT}
        assert arguments[:2] == ["iam", "simulate-principal-policy"]
        context_index = arguments.index("--context-entries")
        assert json.loads(arguments[context_index + 1]) == context
        action = arguments[arguments.index("--action-names") + 1]
        resources = arguments[arguments.index("--resource-arns") + 1:context_index]
        simulated.extend((action, resource) for resource in resources)
        return {"EvaluationResults": [
            {"EvalActionName": action, "EvalResourceName": resource,
             "EvalDecision": expected[action, resource]} for resource in resources
        ]}

    monkeypatch.setattr(check_parameter_access, "aws_json", aws_json)
    check_parameter_access.main()
    assert set(simulated) == set(expected)
    assert "Effective-role IAM simulation passed" in capsys.readouterr().out


def test_verifier_uses_each_resource_decision_in_aws_aggregate_results():
    inside, outside = BASE + PREFIX + "/KEY", BASE + "/outside/KEY"
    cases = [("ssm:GetParameter", inside, "allowed"),
             ("ssm:GetParameter", outside, "explicitDeny")]
    resources = [{"EvalResourceName": inside, "EvalResourceDecision": "allowed"},
                 {"EvalResourceName": outside, "EvalResourceDecision": "explicitDeny"}]
    result = {"EvalActionName": "ssm:GetParameter",
              "EvalResourceName": "arn:aws:ssm:${Region}:${Account}:${Id}",
              "EvalDecision": "explicitDeny", "ResourceSpecificResults": resources}
    verify_results(cases, [result])
    for broken in (resources[:1], resources + [resources[0]],
                   [{**resources[0], "EvalResourceDecision": "explicitDeny"}, resources[1]],
                   [resources[0], {**resources[1], "EvalResourceDecision": "allowed"}],
                   [{**resources[0], "MissingContextValues": ["aws:PrincipalTag/Team"]}, resources[1]]):
        with pytest.raises(ValueError):
            verify_results(cases, [{**result, "ResourceSpecificResults": broken}])
