# Reference "intended state"

`reference.bicep` describes the one resource the detector watches: a single
storage account with a pinned SKU, the portfolio tag set, and three
security-relevant properties. Every watched value is an inline literal — this
template is compiled to `../function/reference_template.json` and shipped inside
the Function.

## Deploy (do this BEFORE `azd up` on the detector)

```bash
az group create -n rg-drift-detector-reference-dev -l eastus2 \
  --tags portfolio=azure-devops-portfolio project=drift-detector environment=dev

az deployment group create -g rg-drift-detector-reference-dev \
  --template-file reference/reference.bicep
```

The detector's `infra/main.bicep` creates a `Reader` role assignment scoped to
`rg-drift-detector-reference-dev`, so that group must exist first.

## After changing this file

Recompile and commit the artifact, or CI will fail:

```bash
az bicep build --file reference/reference.bicep --outfile function/reference_template.json
```
