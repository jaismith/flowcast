import { useState, useEffect } from 'react';
import axios from 'axios';
import {
  AnimatedAxis,
  AnimatedGrid,
  AnimatedLineSeries,
  XYChart,
} from '@visx/xychart';
import { curveNatural } from '@visx/curve';

import './App.css';

const ACCESS_API_ROOT = 'https://y4yzhwsdyvknoliypdkjonn7um0nuodf.lambda-url.us-east-1.on.aws';

type Datapoint = {
  ds: string;
  temp: number | null;
  temp_pred: number | null;
}

function App() {
  const [data, setData] = useState<Datapoint[]>([]);

  useEffect(() => {
    axios.get(ACCESS_API_ROOT)
      .catch(() => console.error('error fetching data from ', ACCESS_API_ROOT))
      .then(res => setData(res!.data as Datapoint[]));
  }, [setData]);

  return (
    <div className="App">
      <XYChart height={300} xScale={{ type: 'band' }} yScale={{ type: 'linear' }}>
        <AnimatedAxis orientation='bottom' />
        <AnimatedAxis orientation='right' />
        <AnimatedGrid columns={false} numTicks={4} />
        <AnimatedLineSeries
          dataKey='historical'
          data={data}
          xAccessor={d => new Date(d.ds)}
          yAccessor={d => d.temp}
          curve={curveNatural}
        />
        <AnimatedLineSeries
          dataKey='pred'
          data={data}
          xAccessor={d => new Date(d.ds)}
          yAccessor={d => d.temp_pred}
          curve={curveNatural}
        />
      </XYChart>
      {JSON.stringify(data)}
    </div>
  );
}

export default App;
