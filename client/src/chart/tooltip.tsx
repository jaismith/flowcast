import styled from 'styled-components';
import { timeFormat } from 'd3-time-format';

import { COLORS } from 'utils/constants';

type TooltipProps = {
  top?: number;
  left?: number;
  date: Date;
  features?: Record<string, unknown>;
};

const TooltipContainer = styled.div`
  border-radius: 3px;
  color: ${COLORS['walnut-brown']}
`;

export const Tooltip = ({ top, left, date, features }: TooltipProps) => (
  <TooltipContainer>
    {timeFormat('%A, %x, %I %p')(date)}
  </TooltipContainer>
);
