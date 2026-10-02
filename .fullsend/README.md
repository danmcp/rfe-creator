# RFE Creator Fullsend Agents

**Note**: Fullsend agents for `rfe-creator` are under development and in a very alpha stage.
They only allow for local execution or a heavily customized workflow. They are not yet
in a state to run them within Fullsend on GitHub or GitLab default Fullsend workflows. You
can find instructions on how to run Fullsend agents locally in the
[fullsend documentation](https://fullsend.sh) and in each agent README's here.

This folder contains the agent implemented for [Fullsend](https://fullsend.sh) that make
use of the RFE Creator skills.

To use this agent run add it to your configuration file with:

```bash
fullsend agent add --fullsend-dir .fullsend https://github.com/opendatahub-io/rfe-creator/blob/main/.fullsend/rfe-creator/rfe-creator.yaml
```

For this change on your `.fullsend/config.yaml` file:

```yaml
agents:
- source: https://raw.githubusercontent.com/opendatahub-io/rfe-creator/<SHA>/.fullsend/rfe-creator/rfe-creator.yaml#sha256=<SHA256>
  ref: main
allowed_remote_resources:
  - https://raw.githubusercontent.com/fullsend-ai/fullsend/
  - https://raw.githubusercontent.com/fullsend-ai/agents/
  - https://raw.githubusercontent.com/opendatahub-io/rfe-creator/

```

More details in the [rfe-creator README](./rfe-creator/README.md).
