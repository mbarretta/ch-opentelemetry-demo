import { Fragment } from 'react';
import { AssistantDemo, AssistantDemoToolCall } from '../../types/Assistant';
import { CypressFields } from '../../utils/enums/CypressFields';
import * as S from './AssistantPanel.styled';

interface IProps {
  demo: AssistantDemo;
  traceId: string;
}

// One tool call as the model made it, for example get_product(product_id=OLJCESPC7Z, currency_code=EUR).
const describe = (tool: AssistantDemoToolCall) => {
  const args = Object.entries(tool.arguments)
    .map(([key, value]) => `${key}=${typeof value === 'string' ? value : JSON.stringify(value)}`)
    .join(', ');
  return `${tool.name}(${args})${tool.ok ? '' : ' (failed)'}`;
};

// What the agent did for this answer, from the response's demo object: nothing here is inferred
// client-side. Collapsed by default; rendered only when ASSISTANT_DEMO_DETAILS is on (the caller checks).
const DemoDetails = ({ demo, traceId }: IProps) => (
  <S.Details data-cy={CypressFields.AssistantDemoDetails}>
    <summary>Demo details</summary>
    <dl>
      <dt>Mode</dt>
      <dd>{demo.mode}</dd>
      <dt>Scenario</dt>
      <dd>{demo.scenario}</dd>
      <dt>Prompt</dt>
      <dd>
        {demo.prompt_version} ({demo.prompt_source})
      </dd>
      <dt>Tools</dt>
      <dd>{demo.tools.length > 0 ? demo.tools.map(describe).join('; ') : 'none'}</dd>
      <dt>Trace</dt>
      <dd>{traceId}</dd>
      {Object.entries(demo.links).map(([label, href]) => (
        <Fragment key={label}>
          <dt>{label}</dt>
          <dd>
            <a href={href} target="_blank" rel="noreferrer">
              {href}
            </a>
          </dd>
        </Fragment>
      ))}
    </dl>
  </S.Details>
);

export default DemoDetails;
