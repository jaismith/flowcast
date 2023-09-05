import { useState, useEffect } from 'react';
import styled from 'styled-components';

import type { Datapoint } from 'utils/types';
import { getForecast } from 'utils/api';
import { Chart } from 'chart/chart';

const Container = styled.div`
  display: flex;
  flex-direction: column;
  align-items: stretch;
  color: #dddddd;
`;

const Navbar = styled.nav`
  display: inline-flex;
  justify-content: space-between;
  padding: 15px 20px;
`;

const CTA = styled.div`
  margin: 10% 20px 5%;
  font-style: italic;
  font-weight: light;
  font-size: 24pt;
`;

export const App = () => {
  const [data, setData] = useState<Datapoint[]>([]);

  useEffect(() => {
    getForecast().then(forecast => setData(forecast))
  }, [setData]);

  return (
    <Container className='App'>
      <Navbar>
        <a href="https://flowcast.jaismith.dev">flowcast</a>
        <a href="https://github.com/jaismith/flowcast">github</a>
      </Navbar>
      <CTA>
        Simple water temperature forecasting, built on 10+ years of USGS and NOAA weather data.
      </CTA>
      {data.length > 1 && <Chart data={data} />}
    </Container>
  );
}

export default App;
