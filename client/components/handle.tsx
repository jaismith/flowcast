import { IconGripVertical } from '@tabler/icons-react';
import { BrushHandleRenderProps } from '@visx/brush/lib/BrushHandle';

const Handle = ({ handle }: {
  handle: BrushHandleRenderProps
}) => {
  const isLeft = handle.x < handle.width / 2;

  return (
    <foreignObject x={handle.x} y={handle.y} width={20} height={handle.height}>
      <div style={{
        background: 'white',
        borderRadius: isLeft ? '10px 5px 10px 5px' : '5px 10px 5px 10px',
        width: '100%',
        height: '100%',
        display: 'flex',
        alignContent: 'center',
        justifyContent: 'center'
      }}>
      <IconGripVertical width={15} />
    </div>
    </foreignObject>
  );
};

export default Handle;
