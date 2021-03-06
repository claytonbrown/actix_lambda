from troposphere import Ref, Template, Output
from troposphere.iam import Role, Policy
from troposphere.awslambda import Function, Code, Environment, Permission
from troposphere import GetAtt, Join
import troposphere.elasticloadbalancingv2 as elb
import troposphere.ec2 as ec2

import boto3
import botocore
import json
import hashlib
from zipfile import ZipFile
import argparse
import configparser
import os.path

parser = argparse.ArgumentParser()
parser.add_argument("AppPath", type=str, help="Path to application")
parser.add_argument("--stack-name", help="CloudFormation stack name. Defaults to name of app")
args = parser.parse_args()

app_config = configparser.ConfigParser()
app_config.read_file(open(os.path.join(args.AppPath, "Cargo.toml")))
app_name = app_config["package"]["name"][1:-1] # Removing quotes either side

BLOCKSIZE = 65536
hasher = hashlib.sha1()
code_path = os.path.join(args.AppPath, "target/x86_64-unknown-linux-musl/release", app_name)
print("Hashing app at %s" % code_path)
with open(code_path, 'rb') as afile:
    buf = afile.read(BLOCKSIZE)
    while len(buf) > 0:
        hasher.update(buf)
        buf = afile.read(BLOCKSIZE)
digest = hasher.hexdigest()

t = Template()
sts_client = boto3.client("sts")
print("Getting AWS account id")
account_id = sts_client.get_caller_identity()["Account"]
s3_client = boto3.client('s3')
bucket_name = f"{account_id}-{app_name}"

try:
    s3_client.head_bucket(Bucket=bucket_name)
except botocore.exceptions.ClientError as e:
    if e.response['ResponseMetadata']['HTTPStatusCode'] == 404:
        s3_client.create_bucket(
            ACL='private',
            Bucket=bucket_name,
            CreateBucketConfiguration={
                'LocationConstraint': 'eu-west-2'
            })
    else:
        raise

try:
    s3_client.head_object(Bucket=bucket_name, Key=digest)
except botocore.exceptions.ClientError as e:
    if e.response['ResponseMetadata']['HTTPStatusCode'] == 404:
        print("Uploading app to S3")
        with ZipFile('code.zip', 'w') as myzip:
            myzip.write(code_path, arcname="bootstrap")
        s3_client.put_object(
            Bucket=bucket_name,
            Key=digest,
            Body=open("code.zip", 'rb')
        )
    else:
        raise

# Create a role for the lambda function
LambdaExecutionRole = t.add_resource(Role(
    "LambdaExecutionRole",
    Path="/",
    Policies=[Policy(
        PolicyName="root",
        PolicyDocument={
            "Version": "2012-10-17",
            "Statement": [{
                "Action": ["logs:*"],
                "Resource": "arn:aws:logs:*:*:*",
                "Effect": "Allow"
            }, {
                "Action": ["lambda:*"],
                "Resource": "*",
                "Effect": "Allow"
            }]
        })],
    AssumeRolePolicyDocument={"Version": "2012-10-17", "Statement": [
        {
            "Action": ["sts:AssumeRole"],
            "Effect": "Allow",
            "Principal": {
                "Service": [
                    "lambda.amazonaws.com",
                    "apigateway.amazonaws.com"
                ]
            }
        }
    ]},
))

# Create the Lambda function
app_function = t.add_resource(Function(
    "AppFunction",
    Code=Code(
        S3Bucket=bucket_name,
        S3Key=digest,
    ),
    Environment=Environment(
        Variables={
            "RUST_BACKTRACE": "1",
            "RUST_LOG": "debug"
        }
    ),
    Handler="not_used",
    Role=GetAtt(LambdaExecutionRole, "Arn"),
    Runtime="provided",
))

ec2_client = boto3.client('ec2')
subnets = ec2_client.describe_subnets()["Subnets"]

targetGroup = t.add_resource(elb.TargetGroup(
    "TargetGroup",
    TargetType="lambda",
    Targets=[elb.TargetDescription(
        Id=GetAtt(app_function, 'Arn')
    )],
    DependsOn="InvokePermission"
))

t.add_resource(Permission(
    "InvokePermission",
    Action="lambda:InvokeFunction",
    FunctionName=GetAtt(app_function, 'Arn'),
    Principal="elasticloadbalancing.amazonaws.com",
    #SourceArn=Ref(targetGroup) # This would create a creation loop
    #SourceAccount=Ref('AWS::AccountId')
))

# Add the application ELB
ApplicationElasticLB = t.add_resource(elb.LoadBalancer(
    "ApplicationElasticLB",
    Name="ApplicationElasticLB",
    Scheme="internet-facing",
    Subnets=[x["SubnetId"] for x in subnets]
))

t.add_resource(elb.Listener(
    "Listener",
    LoadBalancerArn=Ref(ApplicationElasticLB),
    Port=80,
    Protocol="HTTP",
    DefaultActions=[elb.Action(
        Type="forward",
        TargetGroupArn=Ref(targetGroup)
    )]
))

t.add_output([
    Output(
        "LoadbalancerArn",
        Value=Ref(ApplicationElasticLB)
    ),
    Output(
        "LoadbalancerDNSName",
        Value=GetAtt(ApplicationElasticLB, 'DNSName')
    ),
    Output(
        "AppFunctionArn",
        Value=GetAtt(app_function, "Arn")
    ),
])

open("cloud.json", "w").write(t.to_json())

cf = boto3.client('cloudformation')
print("Validating template")
cf.validate_template(TemplateBody=t.to_json())

stack_name = app_name if args.stack_name == None else args.stack_name

# Using the filter functions on describe_stacks makes it fail when there's zero entries...
print("Checking existing CloudFormation stacks")
stacks = [x for x in cf.describe_stacks()["Stacks"] if x["StackName"] == stack_name]

if len(stacks) == 1:
    print("Updating %s stack" % stack_name)
    try:
        stack_result = cf.update_stack(StackName=stack_name, TemplateBody=t.to_json(), Capabilities=['CAPABILITY_IAM'])
        waiter = cf.get_waiter('stack_update_complete')
        waiter.wait(StackName=stack_name)
    except botocore.exceptions.ClientError as e:
        if e.response["Error"]["Message"] == "No updates are to be performed.":
            pass
        else:
            raise
else:
    print("Creating %s stack" % stack_name)
    stack_result = cf.create_stack(StackName=stack_name, TemplateBody=t.to_json(), Capabilities=['CAPABILITY_IAM'])
    waiter = cf.get_waiter('stack_create_complete')
    waiter.wait(StackName=stack_name)

stack = cf.describe_stacks(StackName=stack_name)["Stacks"][0]
outputs = dict([(x["OutputKey"], x["OutputValue"]) for x in stack["Outputs"]])

print(f"{app_name} is deployed at http://{outputs['LoadbalancerDNSName']}")