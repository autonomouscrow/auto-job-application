"""Names of common technologies and methods.

The verifier uses this to notice a skill slipped into a sentence in lower
case ("kubernetes", "react") where the capital-letter check would miss it.
It is a safety net, not a skills taxonomy: a term only matters when it shows
up in generated text without being backed by a cited fact. Names that are
also everyday words ("go", "rest", "spring") are left out; written as names
they carry a capital letter, which the verifier checks separately.
"""

from __future__ import annotations

TERMS = frozenset(
    """
    python java javascript typescript  c++ c#  golang rust kotlin  scala ruby php perl
    matlab  julia haskell ocaml erlang elixir lua  bash  powershell sql nosql html css
    assembly verilog vhdl systemverilog fortran cobol solidity
    react angular vue svelte next.js node.js   django flask fastapi  
    laravel .net asp.net jquery redux graphql  grpc websocket websockets tailwind bootstrap
    android ios flutter xamarin electron qt
    aws azure gcp ec2 s3  dynamodb cloudformation bigquery snowflake databricks redshift
    docker kubernetes k8s helm terraform ansible   jenkins circleci travis gitlab
    github bitbucket git svn mercurial ci/cd devops linux unix  macos
    postgres postgresql mysql sqlite  mongodb redis cassandra elasticsearch kafka rabbitmq
     hadoop  airflow dbt flink presto etl
    pytorch tensorflow keras jax scikit-learn sklearn numpy pandas scipy matplotlib opencv
    xgboost lightgbm huggingface transformers langchain llm llms nlp cuda tensorrt onnx
    reinforcement-learning ppo dqn cnn rnn lstm gan bert gpt rag
    ros ros2 gazebo slam lidar fpga asic rtos  firmware arduino stm32 uart spi i2c 
    simulink solidworks autocad labview plc
    selenium cypress playwright cucumber junit pytest jest mocha testng postman appium
    jira confluence  figma tableau powerbi  vba sap salesforce
     unreal godot opengl vulkan directx webgl blender
    tcp udp http https dns tls ssl oauth jwt ldap kerberos vpn
    multithreading concurrency microservices serverless blockchain cryptography
    kdb  kdb+ bloomberg     arbitrage backtesting
     scrum kanban
    """.split()
)
