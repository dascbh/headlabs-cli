FROM 688128002471.dkr.ecr.us-east-1.amazonaws.com/headlabs-agents:sdk-base
ENV AGENT_ID=finops-advisor PORT=8080
COPY requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt
COPY . /app/agents/finops-advisor
COPY . /app/agents/finops_advisor
EXPOSE 8080
CMD ["python", "-m", "headlabs_sdk.sdk.runtime"]
