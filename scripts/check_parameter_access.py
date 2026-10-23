"""Check effective instance-role access with AWS IAM; never read parameter values."""

import argparse
import json
import re
import subprocess


def access_cases(role_arn, region, prefix):
    match = re.fullmatch(r"arn:(aws(?:-[a-z]+)*):iam::([0-9]{12}):role/[\w+=,.@/-]+", role_arn)
    if not match or not re.fullmatch(r"[a-z]{2}(?:-[a-z]+)+-[0-9]+", region):
        raise ValueError("Invalid role ARN or region")
    if not re.fullmatch(r"(/[a-zA-Z0-9_.-]+)+", prefix):
        raise ValueError("Prefix must be a non-root path without a trailing slash")
    partition, account = match.groups()
    base = f"arn:{partition}:ssm:{region}:{account}:parameter"
    other_account = "222222222222" if account != "222222222222" else "111111111111"
    other_region = "us-west-2" if region != "us-west-2" else "us-east-1"
    other_bases = [base.replace(account, other_account), base.replace(region, other_region)]
    parent = prefix.rsplit("/", 1)[0]
    unrelated = "/outside-access-check" if prefix.split("/")[1] != "outside-access-check" else "/other-check"
    outside = [base + parent + "/OTHER", base + prefix + "-other/KEY",
               base + unrelated + "/KEY", *[b + prefix + "/KEY" for b in other_bases]]
    cases = []
    for action in ("ssm:GetParameter", "ssm:GetParameters"):
        for suffix in ("/KEY", "/nested/KEY"):
            cases.append((action, base + prefix + suffix, "allowed"))
        cases.extend((action, resource, "explicitDeny") for resource in outside)
    cases.extend(("ssm:GetParameterHistory", resource, "explicitDeny") for resource in outside)
    cases.append(("ssm:GetParameterHistory", base + prefix + "/KEY", "implicitDeny"))
    cases.append(("ssm:GetParametersByPath", base + prefix, "allowed"))
    # Test every ancestor, including both root ARN spellings. An ancestor read
    # with Recursive=true could otherwise bypass descendant restrictions.
    ancestors = ["", "/", *[prefix[:i] for i, char in enumerate(prefix) if char == "/" and i]]
    path_resources = [*[base + path for path in ancestors], base + prefix + "-other",
                      base + prefix + "/nested", base + unrelated,
                      *[b + prefix for b in other_bases]]
    cases.extend(("ssm:GetParametersByPath", resource, "explicitDeny") for resource in path_resources)
    for action in ("ssm:UpdateInstanceInformation", "ssm:GetDocument", "ssm:ListInstanceAssociations",
                   "ssmmessages:CreateControlChannel", "ssmmessages:OpenControlChannel",
                   "ssmmessages:CreateDataChannel", "ssmmessages:OpenDataChannel",
                   "ec2messages:GetMessages", "ec2messages:SendReply"):
        cases.append((action, "*", "allowed"))
    return cases


def verify_results(cases, results):
    observed = {}
    for result in results:
        key = result["EvalActionName"], result["EvalResourceName"]
        if key in observed:
            raise ValueError("Duplicate IAM simulation result")
        if result.get("MissingContextValues"):
            raise ValueError("IAM simulation requires additional context")
        observed[key] = result["EvalDecision"]
    for action, resource, expected in cases:
        if observed.get((action, resource)) != expected:
            raise ValueError(f"Unexpected or missing IAM decision for {action}; expected {expected}")


def aws_json(arguments):
    process = subprocess.run(["aws", *arguments, "--output", "json", "--no-cli-pager"],
                             capture_output=True, text=True, timeout=120, check=False)
    if process.returncode:
        raise RuntimeError("AWS CLI request failed; check the identity and IAM simulation permissions")
    return json.loads(process.stdout)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--region", required=True)
    parser.add_argument("--prefix", required=True)
    args = parser.parse_args()
    cases = access_cases(args.role_arn, args.region, args.prefix)
    identity = aws_json(["sts", "get-caller-identity", "--region", args.region])
    if identity["Account"] != args.role_arn.split(":")[4]:
        raise ValueError("Selected AWS identity belongs to a different account")
    # Group requests by action to avoid duplicate cross-product results.
    results = []
    for action in sorted({case[0] for case in cases}):
        resources = sorted({resource for candidate, resource, _ in cases if candidate == action})
        response = aws_json(["iam", "simulate-principal-policy", "--region", args.region,
                             "--policy-source-arn", args.role_arn, "--action-names", action,
                             "--resource-arns", *resources])
        results.extend(response["EvaluationResults"])
    verify_results(cases, results)
    print(f"Effective-role IAM simulation passed: {len(cases)} decisions; no parameter values retrieved.")


if __name__ == "__main__":
    main()
