FROM public.ecr.aws/lambda/python:3.9-arm64

# Copy function code
COPY . ${LAMBDA_TASK_ROOT}

# Install the function's dependencies using file requirements.txt
# from your project folder.
RUN yum update && yum install gcc -y
RUN pip3 install -r ${LAMBDA_TASK_ROOT}/requirements.txt --no-deps --target "${LAMBDA_TASK_ROOT}"
